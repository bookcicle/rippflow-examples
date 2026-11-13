"""Rippflow phase implementations for the multi-repo PR authoring & review pipeline.

All external LLM (`author_pr`, `codex_review`), test-runner (`local_test_suite`), and
human-review (`human_gate`) phases are modeled as `type: async_tool`:
- Each launcher handler validates input, spawns a detached background worker (`start_new_session=True`),
  and returns `{"outcome": "dispatched"}` in milliseconds (`workerCapacityReleased: true`), keeping
  Rippflow handler timeouts at standard 30s-60s (`python-light` / `python-standard`).
- When the external LLM / test job finishes, the detached worker POSTs an authenticated
  `scoped_token_hmac` callback (`AsyncCallbackPayload`) to resume Rippflow execution.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

from .agents import run_author_turn, run_codex_review_turn
from .callback_client import (
    build_callback_payload,
    post_callback,
    redact_secrets,
    write_pending_escalation_envelope,
)
from .models import (
    PipelineConfig,
    RepoSpec,
    RunStore,
    ShardState,
    _resolve_default_workspace,
    build_scoped_git_gh_env,
    detect_default_test_command,
    detect_github_repo_slug,
    load_dotenv_files,
    resolve_gh_token,
    slugify,
)
from .notify import NotificationMessage, send_notification
from .visual_qa import (
    collect_image_files,
    pin_mock_screenshots_to_pr,
    prepare_reference_mock_images,
)


@contextmanager
def _repo_git_lock(src_repo: Path) -> Iterator[None]:
    """Serializes `git fetch` / `git worktree add` against the same backing `.git` directory
    so concurrent runs submitted to `rippflow deploy --local` never collide on `.git/config.lock`.
    """
    lock_dir = Path(tempfile.gettempdir()) / "rippflow-pr-pipeline-git-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"{slugify(str(src_repo.resolve()), max_len=64)}.lock"
    if os.name == "nt":
        import msvcrt

        fh = open(lock_file, "a+", encoding="utf-8")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                fh.close()
    else:
        import fcntl

        fh = open(lock_file, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def _create_isolated_worktree(
    repo_spec: RepoSpec,
    worktree_dest: Path,
    branch_name: str,
    execution_mode: str,
) -> None:
    src = Path(repo_spec.path)
    if not (src / ".git").exists():
        raise FileNotFoundError(f"Repository '{repo_spec.name}' is not a git checkout: {src}")

    with _repo_git_lock(src):
        if execution_mode == "live":
            fetch_refs = [repo_spec.base_branch]
            if repo_spec.resume_branch and repo_spec.resume_branch not in fetch_refs:
                fetch_refs.append(repo_spec.resume_branch)
            subprocess.run(
                ["git", "-C", str(src), "fetch", "origin", *fetch_refs],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )

        # If resuming an existing PR branch, start from `origin/<resume_branch>` or local `<resume_branch>`.
        candidate_refs: list[str] = []
        if repo_spec.resume_branch:
            candidate_refs.extend([f"origin/{repo_spec.resume_branch}", repo_spec.resume_branch])
        candidate_refs.extend([f"origin/{repo_spec.base_branch}", repo_spec.base_branch, "HEAD"])

        base_ref = "HEAD"
        for cand in candidate_refs:
            probe = subprocess.run(
                ["git", "-C", str(src), "rev-parse", "--verify", cand],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0:
                base_ref = cand
                break

        worktree_dest.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(src),
                    "worktree",
                    "add",
                    "--no-track",
                    "-B",
                    branch_name,
                    str(worktree_dest),
                    base_ref,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                break
            time.sleep(0.25 * (attempt + 1))
        else:
            raise RuntimeError(
                f"git worktree add failed for {repo_spec.name}: {proc.stderr or proc.stdout}"
            )


def _create_companion_worktree(
    companion_src: Path,
    companion_dest: Path,
    base_branch: str,
    execution_mode: str,
) -> None:
    if companion_dest.exists():
        return
    if not (companion_src / ".git").exists():
        raise FileNotFoundError(f"Companion repository is not a git checkout: {companion_src}")
    with _repo_git_lock(companion_src):
        if execution_mode == "live":
            subprocess.run(
                ["git", "-C", str(companion_src), "fetch", "origin", base_branch],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
        base_ref = f"origin/{base_branch}"
        probe = subprocess.run(
            ["git", "-C", str(companion_src), "rev-parse", "--verify", base_ref],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            base_ref = "HEAD"
        companion_dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "-C", str(companion_src), "worktree", "add", "--detach", str(companion_dest), base_ref],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git worktree add --detach failed for companion {companion_src.name}: {proc.stderr or proc.stdout}"
            )


def _ensure_shard_hydrated(store: RunStore, cfg: PipelineConfig, request_id: str, shard: str) -> None:
    if shard in cfg.repos:
        return
    repo_path = str((Path(cfg.workspace_dir) / shard).resolve())
    spec = RepoSpec(name=shard, path=repo_path)
    cfg.repos[shard] = spec
    store.save_config(cfg)
    branch = f"{cfg.branch_prefix}/{slugify(cfg.feature_title or cfg.feature_request, 28)}-{request_id.split('-')[0]}"
    wt_path = store.worktrees_dir / shard
    _create_isolated_worktree(spec, wt_path, branch, cfg.execution_mode)
    store.save_shard(
        ShardState(
            shard=shard,
            repo_path=repo_path,
            worktree_path=str(wt_path),
            branch_name=branch,
            base_branch=spec.base_branch,
            github_repo=detect_github_repo_slug(Path(repo_path)),
        )
    )


def _extract_async_shard(request_envelope: dict[str, Any], phase_label: str) -> str:
    inputs_wrapper = request_envelope.get("inputs") or {}
    nested_input = inputs_wrapper.get("input") or {}
    if isinstance(nested_input, dict):
        shard = str(nested_input.get("shard") or "").strip()
        if shard:
            return shard
        # Support `userRerun` wrapped payloads (`{"userRerun": ..., "result": {"shard": ...}}`)
        res = nested_input.get("result")
        if isinstance(res, dict) and res.get("shard"):
            return str(res["shard"]).strip()

    # Fallback if single shard exists in RunStore
    request_id = str(request_envelope.get("workflowId") or "")
    raise ValueError(f"{phase_label} AsyncToolRequest missing inputs.input.shard (workflowId={request_id})")


def _spawn_detached_async_worker(
    *,
    worker_kind: str,
    request_envelope: dict[str, Any],
    repo_root: Path,
    shard: str,
) -> dict[str, Any]:
    """Writes a `0600` job envelope, spawns a detached background worker (`start_new_session=True`),
    and returns `{"outcome": "dispatched"}` immediately so the launcher handler finishes in `< 100ms`.
    """
    request_id = str(request_envelope["workflowId"])
    operation_id = str(request_envelope["operationId"])
    store = RunStore(repo_root, request_id)
    jobs_dir = store.run_dir / "async_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    job_path = jobs_dir / f"{worker_kind}-{shard}-{operation_id}.job.json"
    log_path = jobs_dir / f"{worker_kind}-{shard}-{operation_id}.log"
    ca_cert = os.environ.get("RIPPFLOW_LOCAL_CA_CERT")

    job_record = {
        "schemaVersion": 1,
        "workerKind": worker_kind,
        "shard": shard,
        "caCertPath": ca_cert,
        "requestEnvelope": request_envelope,
    }
    fd = os.open(str(job_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(job_record, fh)
    if os.name != "nt":
        os.chmod(job_path, 0o600)

    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "dispatch.py"),
        "--async-worker",
        worker_kind,
        str(job_path),
    ]
    popen_kwargs: dict[str, Any] = {
        "cwd": str(repo_root),
        "env": os.environ.copy(),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        detached_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        popen_kwargs["creationflags"] = detached_flag
    else:
        # Must start a new session (`setsid`) so Rippflow's `AdapterProcessGroup` cleanup
        # on launcher exit only reaps the short-lived launcher PGID, not the external worker!
        popen_kwargs["start_new_session"] = True

    with open(log_path, "ab") as log_fh:
        subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, **popen_kwargs)

    return {"outcome": "dispatched"}


def run_detached_async_worker(worker_kind: str, job_path: Path, repo_root: Path) -> int:
    """Executes the external LLM / test work out-of-band and POSTs the authenticated HMAC callback."""
    raw = json.loads(job_path.read_text(encoding="utf-8"))
    try:
        job_path.unlink(missing_ok=True)
    except OSError:
        pass

    request_envelope: dict[str, Any] = raw["requestEnvelope"]
    shard = str(raw["shard"])
    ca_cert: str | None = raw.get("caCertPath") or os.environ.get("RIPPFLOW_LOCAL_CA_CERT")
    callback_url = str(request_envelope["callbackUrl"])
    request_id = str(request_envelope["workflowId"])

    try:
        if worker_kind == "author_pr":
            result_payload = _execute_author_pr_job(request_id=request_id, shard=shard, repo_root=repo_root)
        elif worker_kind == "local_test_suite":
            result_payload = _execute_local_test_suite_job(request_id=request_id, shard=shard, repo_root=repo_root)
        elif worker_kind == "codex_review":
            result_payload = _execute_codex_review_job(request_id=request_id, shard=shard, repo_root=repo_root)
        else:
            raise ValueError(f"Unsupported async worker kind '{worker_kind}'")

        cb_payload = build_callback_payload(
            request_envelope,
            status="COMPLETED",
            result_payload=result_payload,
        )
    except Exception as exc:
        safe_msg = redact_secrets(f"{worker_kind} failed on shard '{shard}': {exc}")[:2000]
        print(f"[async-worker:{worker_kind}] ERROR: {safe_msg}", file=sys.stderr, flush=True)
        cb_payload = build_callback_payload(
            request_envelope,
            status="FAILED",
            failure_reason_code=f"{worker_kind.upper()}_WORKER_ERROR",
            failure_message=safe_msg,
        )

    http_code, resp = post_callback(callback_url, cb_payload, ca_cert_path=ca_cert, timeout_sec=15.0)
    if http_code != 200:
        print(
            redact_secrets(f"[async-worker:{worker_kind}] Callback HTTP {http_code}: {resp}"),
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


def handle_prepare_workspace(ctx: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    request_id = str(ctx["requestId"])
    raw_input = ctx.get("payload") or {}
    if not isinstance(raw_input, dict):
        raise ValueError("Workflow input payload must be a JSON object")

    load_dotenv_files(repo_root, repo_root.parent, repo_root.parent.parent)
    cfg = PipelineConfig.from_raw(
        raw_input,
        default_workspace=_resolve_default_workspace(repo_root),
        workflow_dir=repo_root,
    )
    if cfg.author_gh_user == cfg.reviewer_gh_user and cfg.execution_mode == "live":
        raise ValueError(
            f"authorGhUser and reviewerGhUser must be distinct GitHub accounts (both set to '{cfg.author_gh_user}')"
        )

    if cfg.execution_mode == "live":
        # Fail fast before creating worktrees if either GitHub identity is missing from keyring/env.
        resolve_gh_token(cfg.author_gh_user)
        resolve_gh_token(cfg.reviewer_gh_user)

    store = RunStore(repo_root, request_id)
    store.save_config(cfg)
    if cfg.prompt_file and Path(cfg.prompt_file).is_file():
        # Snapshot the exact prompt file into the run artifact dir for full auditability
        (store.run_dir / "prompt.md").write_text(
            Path(cfg.prompt_file).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    short_req = request_id.split("-")[0]
    feat_slug = slugify(cfg.feature_title or cfg.feature_request, max_len=28)

    prepared: list[str] = []
    for name, spec in cfg.repos.items():
        branch = spec.resume_branch or f"{cfg.branch_prefix}/{feat_slug}-{short_req}"
        wt_path = store.worktrees_dir / name
        _create_isolated_worktree(spec, wt_path, branch, cfg.execution_mode)
        for comp in spec.companion_repos:
            comp_name = Path(comp).name
            comp_src = Path(comp) if Path(comp).is_absolute() else (Path(cfg.workspace_dir) / comp).resolve()
            _create_companion_worktree(
                comp_src,
                store.worktrees_dir / comp_name,
                spec.base_branch,
                cfg.execution_mode,
            )
        gh_slug = spec.github_repo or detect_github_repo_slug(Path(spec.path))
        eff_mock_html = spec.mock_html or cfg.mock_html
        eff_mock_shots = list(spec.mock_screenshots or cfg.mock_screenshots)
        mock_imgs: list[Path] = []
        if eff_mock_html or eff_mock_shots:
            mock_imgs = prepare_reference_mock_images(
                mock_screenshots=eff_mock_shots,
                mock_html=eff_mock_html,
                dest_dir=store.run_dir / "visual_qa" / name / "mock",
            )
        shard_state = ShardState(
            shard=name,
            repo_path=spec.path,
            worktree_path=str(wt_path),
            branch_name=branch,
            base_branch=spec.base_branch,
            github_repo=gh_slug,
            mock_screenshot_paths=[str(p) for p in mock_imgs],
        )
        store.save_shard(shard_state)
        prepared.append(name)

    return {
        "outcome": "completed",
        "payload": {
            "requestId": request_id,
            "executionMode": cfg.execution_mode,
            "maxTurns": cfg.max_turns,
            "authorGhUser": cfg.author_gh_user,
            "reviewerGhUser": cfg.reviewer_gh_user,
            "preparedRepos": prepared,
        },
    }


def handle_bind_shard(ctx: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Lightweight `< 50ms` per-shard initializer that binds `shard` into `payload` for `async_tool` phases."""
    request_id = str(ctx["requestId"])
    shard = str(ctx.get("shard") or "").strip()
    if not shard:
        raise ValueError("bind_shard requires a shard name")

    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    _ensure_shard_hydrated(store, cfg, request_id, shard)
    state = store.load_shard(shard)

    return {
        "outcome": "completed",
        "payload": {
            "requestId": request_id,
            "shard": shard,
            "branchName": state.branch_name,
            "worktreePath": state.worktree_path,
        },
    }


def handle_author_pr_async(request_envelope: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "author_pr")
    return _spawn_detached_async_worker(
        worker_kind="author_pr",
        request_envelope=request_envelope,
        repo_root=repo_root,
        shard=shard,
    )


def _execute_author_pr_job(*, request_id: str, shard: str, repo_root: Path) -> dict[str, Any]:
    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    _ensure_shard_hydrated(store, cfg, request_id, shard)

    state = store.load_shard(shard)
    result = run_author_turn(
        cfg=cfg,
        state=state,
        request_id=request_id,
        session_dir=store.sessions_dir,
    )
    state.turns_used = int(result["turn"])
    state.status = "authored"
    state.history.append(
        {
            "turn": state.turns_used,
            "phase": "author_pr",
            "identity": cfg.author_gh_user,
            "summary": result["summary"],
        }
    )
    store.save_shard(state)

    return {
        "shard": shard,
        "turn": state.turns_used,
        "maxTurns": cfg.max_turns,
        "prUrl": state.pr_url,
        "authorIdentity": cfg.author_gh_user,
    }


def handle_local_test_suite_async(request_envelope: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "local_test_suite")
    return _spawn_detached_async_worker(
        worker_kind="local_test_suite",
        request_envelope=request_envelope,
        repo_root=repo_root,
        shard=shard,
    )


def _execute_local_test_suite_job(*, request_id: str, shard: str, repo_root: Path) -> dict[str, Any]:
    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    state = store.load_shard(shard)
    spec = cfg.repos[shard]
    wt = Path(state.worktree_path)

    cmd = spec.test_command or detect_default_test_command(wt)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if (wt / "Cargo.toml").is_file():
        # Always isolate Cargo build artifacts per worktree so concurrent pipeline runs never contend on `~/.cargo/target`!
        env["CARGO_TARGET_DIR"] = str(wt.parent / f".cargo-target-{shard}")
    venv_bin = repo_root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"

    proc = subprocess.run(
        cmd,
        cwd=str(wt),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=780,
        check=False,
    )
    combined_out = redact_secrets(f"{proc.stdout}\n{proc.stderr}".strip())[-8000:]
    passed = proc.returncode == 0
    if passed and cfg.execution_mode == "live":
        ahead_proc = subprocess.run(
            ["git", "-C", str(wt), "rev-list", "--count", f"origin/{state.base_branch}..HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if ahead_proc.returncode == 0 and int((ahead_proc.stdout or "0").strip() or "0") == 0:
            passed = False
            combined_out = (
                f"No commits authored yet on `{state.branch_name}` ahead of `origin/{state.base_branch}` "
                "(e.g. zero matching new feature tests ran). Implement and commit the requested feature + unit tests.\n"
                + combined_out
            )[-8000:]

    # Visual QA screenshot capture step: when `passed` and either `screenshotCommand` is configured
    # OR `mock_screenshot_paths` exist and the worktree defines a `visual_qa_capture` test/script,
    # run screenshot capture into `.runs/<id>/visual_qa/<shard>/turn-<turn>/` and pin to the PR!
    captured_ui_imgs: list[Path] = []
    if passed:
        turn_shot_dir = store.run_dir / "visual_qa" / shard / f"turn-{state.turns_used}"
        turn_shot_dir.mkdir(parents=True, exist_ok=True)
        shot_env = env.copy()
        shot_env["RIPPFLOW_UI_SCREENSHOT_DIR"] = str(turn_shot_dir)

        shot_cmd = spec.screenshot_command
        if shot_cmd is None and state.mock_screenshot_paths and (wt / "Cargo.toml").is_file():
            # Auto-detect GPUI offscreen visual QA screenshot test if authored in the crate
            warp_rs = wt / "src" / "warp_shell.rs"
            if warp_rs.is_file() and "capture_monitor_visual_qa_screenshots" in warp_rs.read_text(
                encoding="utf-8", errors="ignore"
            ):
                shot_cmd = [
                    "cargo",
                    "test",
                    "--locked",
                    "capture_monitor_visual_qa_screenshots",
                    "--",
                    "--ignored",
                    "--nocapture",
                    "--test-threads=1",
                ]

        if shot_cmd is not None:
            shot_proc = subprocess.run(
                shot_cmd,
                cwd=str(wt),
                env=shot_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            shot_out = redact_secrets(f"{shot_proc.stdout}\n{shot_proc.stderr}".strip())[-4000:]
            if shot_proc.returncode != 0:
                passed = False
                combined_out = (
                    f"[Visual QA Screenshot Command Failed (exit {shot_proc.returncode})]\n{shot_out}\n\n{combined_out}"
                )[-8000:]
            else:
                captured_ui_imgs = collect_image_files([turn_shot_dir])
                if not captured_ui_imgs:
                    passed = False
                    combined_out = (
                        f"[Visual QA Screenshot Command Succeeded (exit 0) but wrote 0 PNGs into "
                        f"RIPPFLOW_UI_SCREENSHOT_DIR={turn_shot_dir}]\n{shot_out}\n\n{combined_out}"
                    )[-8000:]
        else:
            captured_ui_imgs = collect_image_files([turn_shot_dir])

        if passed and captured_ui_imgs:
            state.actual_screenshot_paths = [str(p) for p in captured_ui_imgs]
            if cfg.execution_mode == "live" and (state.pr_url or state.pr_number) and state.github_repo:
                try:
                    author_token = resolve_gh_token(cfg.author_gh_user)
                    author_env = build_scoped_git_gh_env(
                        gh_user=cfg.author_gh_user,
                        gh_token=author_token,
                        git_email=cfg.author_git_email,
                    )
                    pinned = pin_mock_screenshots_to_pr(
                        pr_url_or_number=state.pr_url or str(state.pr_number),
                        github_repo=state.github_repo,
                        worktree=wt,
                        mock_images=[Path(p) for p in state.mock_screenshot_paths if Path(p).is_file()],
                        actual_images=captured_ui_imgs,
                        turn=state.turns_used,
                        author_gh_user=cfg.author_gh_user,
                        author_git_email=cfg.author_git_email,
                        author_env=author_env,
                    )
                    if pinned:
                        state.pinned_pr_image_urls = pinned
                except Exception as pin_exc:
                    print(
                        redact_secrets(f"[local_test_suite] Non-fatal PR screenshot pin warning: {pin_exc}"),
                        file=sys.stderr,
                        flush=True,
                    )

    state.last_test_passed = passed
    state.last_test_output = combined_out
    state.history.append(
        {
            "turn": state.turns_used,
            "phase": "local_test_suite",
            "command": cmd,
            "passed": passed,
            "capturedScreenshots": len(captured_ui_imgs),
        }
    )

    if passed:
        state.status = "tests_passed"
        store.save_shard(state)
        return {
            "shard": shard,
            "turn": state.turns_used,
            "testsPassed": True,
            "nextDecision": "completed",
        }

    if state.turns_used < cfg.max_turns:
        store.save_shard(state)
        return {
            "shard": shard,
            "turn": state.turns_used,
            "testsPassed": False,
            "reason": "local_tests_failed",
            "nextDecision": "rework",
        }

    state.status = "escalated"
    state.escalation_reason = f"Reached {cfg.max_turns}-turn limit with failing local test suite."
    store.save_shard(state)
    return {
        "shard": shard,
        "turn": state.turns_used,
        "testsPassed": False,
        "status": "escalated",
        "escalationReason": state.escalation_reason,
        "nextDecision": "completed",
    }


def handle_route_test_outcome(ctx: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Fast `< 50ms` synchronous router that maps `local_test_suite` callback result to `completed` vs `rework`."""
    request_id = str(ctx["requestId"])
    shard = str(ctx.get("shard") or "")
    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    state = store.load_shard(shard)

    if state.last_test_passed:
        return {
            "outcome": "completed",
            "payload": {
                "shard": shard,
                "turn": state.turns_used,
                "testsPassed": True,
                "prUrl": state.pr_url,
            },
        }

    if state.status != "escalated" and state.turns_used < cfg.max_turns:
        return {
            "outcome": "rework",
            "payload": {
                "shard": shard,
                "turn": state.turns_used,
                "testsPassed": False,
                "reason": "local_tests_failed",
                "prUrl": state.pr_url,
            },
        }

    return {
        "outcome": "completed",
        "payload": {
            "shard": shard,
            "turn": state.turns_used,
            "testsPassed": False,
            "status": "escalated",
            "escalationReason": state.escalation_reason,
            "prUrl": state.pr_url,
        },
    }


def handle_codex_review_async(request_envelope: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "codex_review")
    return _spawn_detached_async_worker(
        worker_kind="codex_review",
        request_envelope=request_envelope,
        repo_root=repo_root,
        shard=shard,
    )


def _execute_codex_review_job(*, request_id: str, shard: str, repo_root: Path) -> dict[str, Any]:
    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    state = store.load_shard(shard)

    if state.status == "escalated":
        return {
            "shard": shard,
            "turn": state.turns_used,
            "status": "escalated",
            "escalationReason": state.escalation_reason,
            "prUrl": state.pr_url,
            "nextDecision": "completed",
        }

    review = run_codex_review_turn(cfg=cfg, state=state)
    verdict = review["verdict"]
    comments = review["comments"]
    state.last_review_verdict = verdict
    state.last_review_summary = review["summary"]
    state.pending_review_comments = comments
    if review.get("sessionId"):
        state.codex_session_id = str(review["sessionId"])
    if review.get("headOid"):
        state.last_reviewed_commit = str(review["headOid"])
    state.review_ledger.append(
        {
            "turn": state.turns_used,
            "commitOid": review.get("headOid"),
            "verdict": verdict,
            "summary": review["summary"],
            "comments": comments,
        }
    )
    state.history.append(
        {
            "turn": state.turns_used,
            "phase": "codex_review",
            "identity": cfg.reviewer_gh_user,
            "verdict": verdict,
            "commentCount": len(comments),
            "ghAction": review["ghAction"],
            "sessionId": state.codex_session_id,
        }
    )

    if verdict == "approve":
        state.status = "approved"
        store.save_shard(state)
        return {
            "shard": shard,
            "turn": state.turns_used,
            "status": "approved",
            "reviewerIdentity": cfg.reviewer_gh_user,
            "prUrl": state.pr_url,
            "nextDecision": "completed",
        }

    if state.turns_used < cfg.max_turns:
        state.status = "changes_requested"
        store.save_shard(state)
        return {
            "shard": shard,
            "turn": state.turns_used,
            "status": "changes_requested",
            "commentCount": len(comments),
            "prUrl": state.pr_url,
            "nextDecision": "rework",
        }

    state.status = "escalated"
    state.escalation_reason = (
        f"Reached {cfg.max_turns}-turn cap with {len(comments)} unresolved review comment(s) from {cfg.reviewer_gh_user}."
    )
    store.save_shard(state)
    return {
        "shard": shard,
        "turn": state.turns_used,
        "status": "escalated",
        "escalationReason": state.escalation_reason,
        "commentCount": len(comments),
        "prUrl": state.pr_url,
        "nextDecision": "completed",
    }


def handle_route_review_outcome(ctx: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Fast `< 50ms` synchronous router that maps `codex_review` callback result to `completed` vs `rework`."""
    request_id = str(ctx["requestId"])
    shard = str(ctx.get("shard") or "")
    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    state = store.load_shard(shard)

    if state.status == "approved":
        return {
            "outcome": "completed",
            "payload": {
                "shard": shard,
                "turn": state.turns_used,
                "status": "approved",
                "reviewerIdentity": cfg.reviewer_gh_user,
                "prUrl": state.pr_url,
            },
        }

    if state.status != "escalated" and state.turns_used < cfg.max_turns:
        return {
            "outcome": "rework",
            "payload": {
                "shard": shard,
                "turn": state.turns_used,
                "status": "changes_requested",
                "commentCount": len(state.pending_review_comments),
                "prUrl": state.pr_url,
            },
        }

    return {
        "outcome": "completed",
        "payload": {
            "shard": shard,
            "turn": state.turns_used,
            "status": "escalated",
            "escalationReason": state.escalation_reason,
            "commentCount": len(state.pending_review_comments),
            "prUrl": state.pr_url,
        },
    }


def handle_human_gate_async(request_envelope: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """`type: async_tool` handler: auto-completes approved shards; pauses & notifies OS on 15-turn escalations."""
    request_id = str(request_envelope["workflowId"])
    shard = _extract_async_shard(request_envelope, "human_gate")

    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    state = store.load_shard(shard)
    callback_url = str(request_envelope["callbackUrl"])
    ca_cert = os.environ.get("RIPPFLOW_LOCAL_CA_CERT")

    if state.status == "approved":
        cb_payload = build_callback_payload(
            request_envelope,
            status="COMPLETED",
            result_payload={
                "shard": shard,
                "resolution": "auto_approved",
                "turnsUsed": state.turns_used,
                "prUrl": state.pr_url,
            },
        )
        http_code, resp = post_callback(callback_url, cb_payload, ca_cert_path=ca_cert)
        if http_code != 200:
            raise RuntimeError(f"human_gate auto-complete callback failed ({http_code}): {resp}")
        return {"outcome": "dispatched"}

    # Escalation path: fire multi-OS desktop notification + persist `0600` envelope for human resolution!
    reason = state.escalation_reason or f"Turn limit ({cfg.max_turns}) reached without approval."
    notif = NotificationMessage(
        title="Rippflow PR Escalation — Human Review Needed",
        subtitle=f"{shard} (Turn {state.turns_used}/{cfg.max_turns})",
        body=reason,
        url=state.pr_url,
        urgency="critical",
    )
    receipt = send_notification(
        notif,
        alert_log_path=store.escalations_dir / "notifications.jsonl",
        enable_os_popup=cfg.enable_os_notifications,
    )

    envelope_path = store.escalations_dir / f"{shard}.callback.json"
    resolve_cmd = (
        f"python3 scripts/resolve_gate.py --run-id {request_id} --shard {shard} --decision approve"
    )
    write_pending_escalation_envelope(
        envelope_path,
        request_envelope=request_envelope,
        ca_cert_path=ca_cert,
        metadata={
            "shard": shard,
            "turnsUsed": state.turns_used,
            "maxTurns": cfg.max_turns,
            "prUrl": state.pr_url,
            "escalationReason": reason,
            "notificationReceipt": receipt.to_dict(),
            "resolveCommand": resolve_cmd,
        },
    )
    print(
        f"[HUMAN_GATE] Escalated shard '{shard}' after {state.turns_used} turns "
        f"(notify={receipt.backend}). Resolve via: {resolve_cmd}",
        file=sys.stderr,
    )
    return {"outcome": "dispatched"}


def handle_publish_summary(ctx: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    request_id = str(ctx["requestId"])
    store = RunStore(repo_root, request_id)
    cfg = store.load_config()
    shards = store.load_all_shards()

    report_items = []
    for s in shards:
        report_items.append(
            {
                "repo": s.shard,
                "branch": s.branch_name,
                "prUrl": s.pr_url,
                "turnsUsed": s.turns_used,
                "status": s.status,
                "escalationReason": s.escalation_reason,
                "humanResolution": s.human_resolution,
            }
        )

    summary = {
        "requestId": request_id,
        "featureRequest": cfg.feature_request,
        "authorGhUser": cfg.author_gh_user,
        "reviewerGhUser": cfg.reviewer_gh_user,
        "maxTurns": cfg.max_turns,
        "repositories": report_items,
    }
    summary_path = store.run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return {
        "outcome": "completed",
        "payload": summary,
    }
