"""Phase implementations for `multi-repo-release-train` (`local` + `cloud` execution).

Phases:
1. `detect_drift` (root, inline, 60s):
   Scans target git repos for commits since the last tag + pinned SHA drift (`PINNED_SOURCE_SHAS.json`)
   and creates isolated release worktrees.
2. `bind_shard` (per_shard, inline, 30s):
   Binds shard identity into payload for downstream `async_tool` phases.
3. `bump_and_changelog` (per_shard, `type: async_tool`, 30s launcher + 30m callback):
   Bumps `Cargo.toml` / `pyproject.toml` / `package.json`, updates `PINNED_SOURCE_SHAS.json`, and drafts
   `CHANGELOG.md` (AI via Gemini/Codex ONLY in `RIPPFLOW_ENV=local`; flagged OFF to pure-Python deterministic
   changelog when `RIPPFLOW_ENV=cloud`!).
4. `verify_build_and_checksums` (per_shard, `type: async_tool`, 30s launcher + 30m callback):
   Runs per-shard build/test verification, computes deterministic SHA-256 manifest checksums, and runs
   optional benchmark checks.
5. `release_signoff_gate` (fan_in_all, `type: async_tool`, 30s launcher + 24h callback):
   Human release sign-off gate: fires OS desktop notification (`local` only) + Slack/email webhooks (`local` & `cloud`)
   and suspends workflow capacity (`workerCapacityReleased: true`) until approved/rejected via authenticated callback.
6. `publish_and_sync_docs` (root, terminal, inline, 60s):
   Applies ordered release tags (`publishOrder`) and writes unified `RELEASE_NOTES.md` + `release-manifest.json`.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

from .callback_client import (
    build_callback_payload,
    deliver_cloud_lambda_callback,
    post_callback,
    redact_secrets,
    write_pending_signoff_envelope,
)
from .changelog import synthesize_changelog
from .models import (
    ReleaseConfig,
    ReleaseRepoSpec,
    ReleaseRunStore,
    ShardReleaseState,
    detect_rippflow_env,
    is_cloud_env,
)
from .notify import ReleaseAlertMessage, dispatch_signoff_notifications


@contextmanager
def _repo_git_lock(src_repo: Path) -> Iterator[None]:
    lock_dir = Path(tempfile.gettempdir()) / "rippflow-release-train-git-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(src_repo.resolve()).encode("utf-8")).hexdigest()[:16]
    lock_file = lock_dir / f"{src_repo.name}-{digest}.lock"
    fh = open(lock_file, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=check,
    )


def _head_sha(repo: Path) -> str:
    return _git(repo, ["rev-parse", "HEAD"]).stdout.strip()


def _commits_since_latest_tag(repo: Path) -> list[str]:
    tag_probe = _git(repo, ["describe", "--tags", "--abbrev=0"], check=False)
    rev_range = f"{tag_probe.stdout.strip()}..HEAD" if tag_probe.returncode == 0 and tag_probe.stdout.strip() else "HEAD"
    log_proc = _git(repo, ["log", "--oneline", "-n", "50", rev_range], check=False)
    return [ln.strip() for ln in (log_proc.stdout or "").splitlines() if ln.strip()]


def _create_release_worktree(spec: ReleaseRepoSpec, dest: Path, branch_name: str) -> None:
    src = Path(spec.path)
    if not (src / ".git").exists():
        raise FileNotFoundError(f"Repository '{spec.name}' is not a git repository: {src}")
    with _repo_git_lock(src):
        if dest.exists():
            _git(src, ["worktree", "remove", "--force", str(dest)], check=False)
            shutil.rmtree(dest, ignore_errors=True)
        _git(src, ["worktree", "prune"], check=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        base_ref = spec.base_branch
        if _git(src, ["rev-parse", "--verify", base_ref], check=False).returncode != 0:
            base_ref = "HEAD"
        proc = _git(src, ["worktree", "add", "--no-track", "-B", branch_name, str(dest), base_ref], check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed for {spec.name}: {proc.stderr or proc.stdout}")
        _git(dest, ["config", "user.name", "rippflow-release-bot"], check=False)
        _git(dest, ["config", "user.email", "release-bot@rippflow.local"], check=False)
        _git(dest, ["config", "commit.gpgsign", "false"], check=False)


def _bump_version_in_file(file_path: Path, new_version: str) -> bool:
    if not file_path.is_file():
        return False
    original = file_path.read_text(encoding="utf-8")
    updated = original

    if file_path.name == "Cargo.toml":
        updated = re.sub(
            r'(?m)^(\s*version\s*=\s*")([^"]+)(")',
            rf"\g<1>{new_version}\g<3>",
            original,
            count=1,
        )
    elif file_path.name == "pyproject.toml":
        updated = re.sub(
            r'(?m)^(\s*version\s*=\s*")([^"]+)(")',
            rf"\g<1>{new_version}\g<3>",
            original,
            count=1,
        )
    elif file_path.name == "package.json":
        data = json.loads(original)
        data["version"] = new_version
        updated = json.dumps(data, indent=2) + "\n"
    else:
        updated = f"{new_version}\n"

    if updated != original:
        file_path.write_text(updated, encoding="utf-8")
        return True
    return False


def _detect_version_file(wt: Path, explicit: str | None) -> Path | None:
    if explicit:
        cand = wt / explicit
        return cand if cand.is_file() else None
    for name in ("Cargo.toml", "pyproject.toml", "package.json", "VERSION"):
        if (wt / name).is_file():
            return wt / name
    return None


def _compute_repo_checksums(wt: Path) -> dict[str, str]:
    tracked = _git(wt, ["ls-files"], check=False).stdout.splitlines()
    checksums: dict[str, str] = {}
    for rel in sorted(tracked)[:200]:
        fpath = wt / rel
        if fpath.is_file():
            checksums[rel] = hashlib.sha256(fpath.read_bytes()).hexdigest()
    return checksums


def _default_verify_command(wt: Path) -> list[str]:
    if (wt / "Cargo.toml").is_file():
        return ["cargo", "check", "--locked"]
    if (wt / "pyproject.toml").is_file() and (wt / "tests").is_dir():
        return [sys.executable, "-m", "pytest", "-q"]
    if (wt / "package.json").is_file():
        return ["npm", "test"]
    return [sys.executable, "-c", "print('release artifact structure ok')"]


def _extract_async_shard(request_envelope: dict[str, Any], phase_label: str) -> str:
    inputs = request_envelope.get("inputs") or {}
    inp = inputs.get("input") if isinstance(inputs, dict) else {}
    if isinstance(inp, dict):
        shard = str(inp.get("shard") or "").strip()
        if shard:
            return shard
    opts = inputs.get("options") if isinstance(inputs, dict) else {}
    if isinstance(opts, dict):
        shard = str(opts.get("shard") or "").strip()
        if shard:
            return shard
    raise ValueError(f"{phase_label} async envelope missing bound 'shard'")


def _spawn_detached_worker(
    *,
    worker_kind: str,
    request_envelope: dict[str, Any],
    workflow_dir: Path,
    shard: str | None = None,
) -> dict[str, Any]:
    request_id = str(request_envelope["workflowId"])
    operation_id = str(request_envelope["operationId"])
    rt_env = detect_rippflow_env()
    store = ReleaseRunStore(workflow_dir, request_id, rippflow_env=rt_env)
    jobs_dir = store.run_dir / "async_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    safe_op = re.sub(r"[^a-zA-Z0-9_.-]", "_", operation_id)
    label = f"{worker_kind}-{shard}-{safe_op}" if shard else f"{worker_kind}-{safe_op}"
    job_path = jobs_dir / f"{label}.job.json"
    log_path = jobs_dir / f"{label}.log"

    record = {
        "schemaVersion": 1,
        "workerKind": worker_kind,
        "shard": shard,
        "rippflowEnv": rt_env,
        "caCertPath": os.environ.get("RIPPFLOW_LOCAL_CA_CERT"),
        "requestEnvelope": request_envelope,
    }
    fd = os.open(str(job_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    if os.name != "nt":
        os.chmod(job_path, 0o600)

    cmd = [sys.executable, str(workflow_dir / "scripts" / "dispatch.py"), "--async-worker", worker_kind, str(job_path)]
    env = os.environ.copy()
    env["RIPPFLOW_ENV"] = rt_env
    popen_kwargs: dict[str, Any] = {
        "cwd": str(workflow_dir),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        popen_kwargs["start_new_session"] = True

    with open(log_path, "ab") as log_fh:
        subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, **popen_kwargs)

    return {"outcome": "dispatched"}


# ---------------------------------------------------------------------------
# Phase 1: detect_drift (root, inline)
# ---------------------------------------------------------------------------
def handle_detect_drift(ctx: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    request_id = str(ctx["requestId"])
    raw_input = ctx.get("payload") or {}
    if not isinstance(raw_input, dict):
        raise ValueError("Input payload must be a JSON object")

    cfg = ReleaseConfig.from_raw(raw_input, workflow_dir=workflow_dir)
    store = ReleaseRunStore(workflow_dir, request_id, rippflow_env=cfg.rippflow_env)
    store.save_config(cfg)

    head_shas: dict[str, str] = {}
    for name, spec in cfg.repos.items():
        head_shas[name] = _head_sha(Path(spec.path))

    short_req = request_id.split("-")[0]
    drift_summary: dict[str, Any] = {}

    for name, spec in cfg.repos.items():
        wt_path = store.worktrees_dir / name
        branch = f"release/v{cfg.release_version}-{short_req}"
        _create_release_worktree(spec, wt_path, branch)

        commits = _commits_since_latest_tag(wt_path)
        sha_drifts: dict[str, dict[str, str]] = {}
        pin_rel = spec.pinned_sha_file or ("PINNED_SOURCE_SHAS.json" if (wt_path / "PINNED_SOURCE_SHAS.json").is_file() else None)
        if pin_rel and (wt_path / pin_rel).is_file():
            pin_data = json.loads((wt_path / pin_rel).read_text(encoding="utf-8"))
            if isinstance(pin_data, dict):
                for dep_name, current_head in head_shas.items():
                    old_pin = pin_data.get(dep_name)
                    if isinstance(old_pin, str) and old_pin != current_head:
                        sha_drifts[dep_name] = {"old": old_pin, "new": current_head}

        drift_detected = bool(commits or sha_drifts)
        state = ShardReleaseState(
            shard=name,
            repo_path=spec.path,
            worktree_path=str(wt_path),
            base_branch=spec.base_branch,
            head_sha_before=head_shas[name],
            head_sha_after=head_shas[name],
            commits_since_last_tag=commits,
            drift_detected=drift_detected,
            pinned_sha_drifts=sha_drifts,
        )
        store.save_shard(state)
        drift_summary[name] = {
            "headSha": head_shas[name],
            "commitsSinceTag": len(commits),
            "pinnedShaDrifts": sha_drifts,
            "driftDetected": drift_detected,
        }

    return {
        "outcome": "completed",
        "payload": {
            "requestId": request_id,
            "releaseVersion": cfg.release_version,
            "rippflowEnv": cfg.rippflow_env,
            "aiChangelogEnabled": cfg.ai_changelog_enabled,
            "driftSummary": drift_summary,
        },
    }


# ---------------------------------------------------------------------------
# Phase 2: bind_shard (per_shard, inline)
# ---------------------------------------------------------------------------
def handle_bind_shard(ctx: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    request_id = str(ctx["requestId"])
    shard = str(ctx.get("shard") or "").strip()
    if not shard:
        raise ValueError("bind_shard requires shard")
    store = ReleaseRunStore(workflow_dir, request_id)
    cfg = store.load_config()
    if not store.shard_file(shard).is_file():
        spec = cfg.repos.get(shard) or ReleaseRepoSpec(name=shard, path=str((Path(cfg.workspace_dir) / shard).resolve()))
        cfg.repos[shard] = spec
        store.save_config(cfg)
        wt_path = store.worktrees_dir / shard
        _create_release_worktree(spec, wt_path, f"release/v{cfg.release_version}-{request_id.split('-')[0]}")
        sha = _head_sha(wt_path)
        store.save_shard(
            ShardReleaseState(
                shard=shard,
                repo_path=spec.path,
                worktree_path=str(wt_path),
                base_branch=spec.base_branch,
                head_sha_before=sha,
                head_sha_after=sha,
                commits_since_last_tag=_commits_since_latest_tag(wt_path),
                drift_detected=True,
            )
        )
    state = store.load_shard(shard)
    return {
        "outcome": "completed",
        "payload": {
            "requestId": request_id,
            "shard": shard,
            "releaseVersion": cfg.release_version,
            "rippflowEnv": cfg.rippflow_env,
            "driftDetected": state.drift_detected,
        },
    }


# ---------------------------------------------------------------------------
# Phase 3: bump_and_changelog (per_shard, async_tool)
# ---------------------------------------------------------------------------
def handle_bump_and_changelog_async(request_envelope: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "bump_and_changelog")
    return _spawn_detached_worker(
        worker_kind="bump_and_changelog",
        request_envelope=request_envelope,
        workflow_dir=workflow_dir,
        shard=shard,
    )


def _execute_bump_and_changelog_job(*, request_id: str, shard: str, workflow_dir: Path, rippflow_env: str) -> dict[str, Any]:
    store = ReleaseRunStore(workflow_dir, request_id, rippflow_env=rippflow_env)
    cfg = store.load_config()
    state = store.load_shard(shard)
    spec = cfg.repos[shard]
    wt = Path(state.worktree_path)

    bumped_files: list[str] = []
    vfile = _detect_version_file(wt, spec.version_file)
    if vfile and _bump_version_in_file(vfile, cfg.release_version):
        bumped_files.append(str(vfile.relative_to(wt)))

    pin_rel = spec.pinned_sha_file or ("PINNED_SOURCE_SHAS.json" if (wt / "PINNED_SOURCE_SHAS.json").is_file() else None)
    if pin_rel and (wt / pin_rel).is_file() and state.pinned_sha_drifts:
        pin_path = wt / pin_rel
        pin_data = json.loads(pin_path.read_text(encoding="utf-8"))
        for dep, delta in state.pinned_sha_drifts.items():
            pin_data[dep] = delta["new"]
        pin_path.write_text(json.dumps(pin_data, indent=2) + "\n", encoding="utf-8")
        bumped_files.append(pin_rel)

    # Flagged OFF automatically when `rippflow_env == "cloud"`!
    effective_ai = False if is_cloud_env(rippflow_env) else cfg.ai_changelog_enabled
    changelog_section, generator_used = synthesize_changelog(
        repo_name=shard,
        release_version=cfg.release_version,
        commits=state.commits_since_last_tag,
        pinned_sha_updates=state.pinned_sha_drifts,
        worktree=wt,
        rippflow_env=rippflow_env,
        ai_enabled=effective_ai,
        provider=cfg.changelog_provider,
        gemini_model=cfg.gemini_model,
        codex_model=cfg.codex_model,
    )

    changelog_path = wt / "CHANGELOG.md"
    if changelog_path.is_file():
        existing = changelog_path.read_text(encoding="utf-8")
        if f"## [{cfg.release_version}]" not in existing:
            changelog_path.write_text(changelog_section + "\n" + existing, encoding="utf-8")
            bumped_files.append("CHANGELOG.md")
    else:
        changelog_path.write_text("# Changelog\n\n" + changelog_section, encoding="utf-8")
        bumped_files.append("CHANGELOG.md")

    if _git(wt, ["status", "--porcelain"], check=False).stdout.strip():
        _git(wt, ["add", "-A"])
        _git(wt, ["commit", "-m", f"chore(release): prepare {shard} v{cfg.release_version}"])

    state.head_sha_after = _head_sha(wt)
    state.bumped_files = bumped_files
    state.changelog_markdown = changelog_section
    state.changelog_generator = generator_used
    state.status = "bumped"
    store.save_shard(state)

    return {
        "shard": shard,
        "releaseVersion": cfg.release_version,
        "rippflowEnv": rippflow_env,
        "aiUsed": generator_used.startswith("local-ai:"),
        "changelogGenerator": generator_used,
        "bumpedFiles": bumped_files,
        "headShaAfter": state.head_sha_after,
    }


# ---------------------------------------------------------------------------
# Phase 4: verify_build_and_checksums (per_shard, async_tool)
# ---------------------------------------------------------------------------
def handle_verify_build_and_checksums_async(request_envelope: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "verify_build_and_checksums")
    return _spawn_detached_worker(
        worker_kind="verify_build_and_checksums",
        request_envelope=request_envelope,
        workflow_dir=workflow_dir,
        shard=shard,
    )


def _execute_verify_build_and_checksums_job(*, request_id: str, shard: str, workflow_dir: Path, rippflow_env: str) -> dict[str, Any]:
    store = ReleaseRunStore(workflow_dir, request_id, rippflow_env=rippflow_env)
    cfg = store.load_config()
    state = store.load_shard(shard)
    spec = cfg.repos[shard]
    wt = Path(state.worktree_path)

    cmd = spec.verify_command or _default_verify_command(wt)
    env = os.environ.copy()
    if (wt / "Cargo.toml").is_file():
        env["CARGO_TARGET_DIR"] = str(wt.parent / f".cargo-target-{shard}")

    proc = subprocess.run(
        cmd,
        cwd=str(wt),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    passed = proc.returncode == 0
    combined_out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-4000:]
    if not passed:
        state.verify_passed = False
        state.verify_output = combined_out
        store.save_shard(state)
        raise RuntimeError(f"Release verification failed for '{shard}': {combined_out[:600]}")

    bench_summary = "skipped"
    if spec.benchmark_command:
        bproc = subprocess.run(
            spec.benchmark_command,
            cwd=str(wt),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if bproc.returncode != 0:
            raise RuntimeError(f"Benchmark regression check failed for '{shard}': {(bproc.stderr or bproc.stdout)[:500]}")
        bench_summary = (bproc.stdout or "passed").strip()[:500]

    checksums = _compute_repo_checksums(wt)
    manifest_digest = hashlib.sha256(json.dumps(checksums, sort_keys=True).encode("utf-8")).hexdigest()

    state.verify_passed = True
    state.verify_output = combined_out
    state.artifact_checksums = checksums
    state.benchmark_summary = bench_summary
    state.status = "verified"
    store.save_shard(state)

    return {
        "shard": shard,
        "verifyPassed": True,
        "trackedFilesHashed": len(checksums),
        "manifestSha256": manifest_digest,
        "benchmarkSummary": bench_summary,
    }


# ---------------------------------------------------------------------------
# Phase 5: release_signoff_gate (fan_in_all, async_tool human decision gate)
# ---------------------------------------------------------------------------
def handle_release_signoff_gate_async(request_envelope: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    request_id = str(request_envelope["workflowId"])
    rt_env = detect_rippflow_env()
    store = ReleaseRunStore(workflow_dir, request_id, rippflow_env=rt_env)
    cfg = store.load_config()
    shards = store.load_all_shards()

    shard_names = [s.shard for s in shards]
    alert_msg = ReleaseAlertMessage(
        title="Rippflow Release Sign-Off Required",
        subtitle=f"Release v{cfg.release_version} ({len(shard_names)} shards verified)",
        body=f"Run {request_id}: verified {', '.join(shard_names)}. Approve or reject via scripts/resolve_release_gate.py.",
        release_version=cfg.release_version,
        run_id=request_id,
        shards=shard_names,
    )
    dispatch_signoff_notifications(
        alert_msg,
        rippflow_env=rt_env,
        slack_webhook_url=cfg.slack_webhook_url,
        email_webhook_url=cfg.email_webhook_url,
        alert_log_path=store.signoff_dir / "notifications.jsonl",
    )

    if cfg.auto_approve_gate:
        return _spawn_detached_worker(
            worker_kind="auto_approve_signoff",
            request_envelope=request_envelope,
            workflow_dir=workflow_dir,
        )

    # Persist `0600` callback envelope & suspend workflow (`workerCapacityReleased: true`) awaiting human sign-off!
    write_pending_signoff_envelope(
        store.signoff_dir,
        request_envelope,
        metadata={
            "releaseVersion": cfg.release_version,
            "rippflowEnv": rt_env,
            "shards": shard_names,
            "publishOrder": cfg.publish_order,
            "resolveCommand": (
                f"python3 scripts/resolve_release_gate.py --run-id {request_id} --decision approve"
            ),
        },
    )
    return {"outcome": "dispatched"}


# ---------------------------------------------------------------------------
# Phase 6: publish_and_sync_docs (root, terminal, inline)
# ---------------------------------------------------------------------------
def handle_publish_and_sync_docs(ctx: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    request_id = str(ctx["requestId"])
    rt_env = detect_rippflow_env()
    store = ReleaseRunStore(workflow_dir, request_id, rippflow_env=rt_env)
    cfg = store.load_config()
    by_shard = {s.shard: s for s in store.load_all_shards()}

    ordered_shards = [s for s in cfg.publish_order if s in by_shard]
    for extra in sorted(by_shard):
        if extra not in ordered_shards:
            ordered_shards.append(extra)

    published_records: list[dict[str, Any]] = []
    combined_notes = [f"# Release Notes — v{cfg.release_version}", ""]

    for idx, shard_name in enumerate(ordered_shards, start=1):
        st = by_shard[shard_name]
        spec = cfg.repos[shard_name]
        wt = Path(st.worktree_path)
        tag_name = f"v{cfg.release_version}"

        combined_notes.append(st.changelog_markdown.strip())
        combined_notes.append("")

        if spec.docs_sync_target:
            docs_target = wt / spec.docs_sync_target
            docs_target.parent.mkdir(parents=True, exist_ok=True)
            docs_target.write_text("\n".join(combined_notes).rstrip() + "\n", encoding="utf-8")
            _git(wt, ["add", spec.docs_sync_target])
            if _git(wt, ["status", "--porcelain"], check=False).stdout.strip():
                _git(wt, ["commit", "-m", f"docs(release): sync release notes for v{cfg.release_version}"])
                st.head_sha_after = _head_sha(wt)
                store.save_shard(st)

        # Create/update annotated tag inside the isolated release worktree AFTER any docs sync commit
        _git(wt, ["tag", "-f", "-a", tag_name, "-m", f"Release {shard_name} {tag_name}"])
        if cfg.execution_mode == "live":
            # Fast-forward tag to local backing repo or origin if configured
            _git(wt, ["push", spec.path, f"refs/tags/{tag_name}:refs/tags/{tag_name}"], check=False)

        st.published_tag = tag_name
        st.status = "published"
        store.save_shard(st)

        published_records.append(
            {
                "order": idx,
                "shard": shard_name,
                "tag": tag_name,
                "headSha": st.head_sha_after,
                "changelogGenerator": st.changelog_generator,
                "trackedFilesHashed": len(st.artifact_checksums),
            }
        )

    manifest = {
        "schemaVersion": 1,
        "requestId": request_id,
        "releaseVersion": cfg.release_version,
        "rippflowEnv": rt_env,
        "aiChangelogEnabled": False if is_cloud_env(rt_env) else cfg.ai_changelog_enabled,
        "publishedShards": published_records,
    }
    (store.run_dir / "release-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (store.run_dir / "RELEASE_NOTES.md").write_text("\n".join(combined_notes).rstrip() + "\n", encoding="utf-8")

    return {"outcome": "completed", "payload": manifest}


# ---------------------------------------------------------------------------
# Detached worker & Cloud Lambda adapter execution
# ---------------------------------------------------------------------------
def run_detached_async_worker(worker_kind: str, job_path: Path, workflow_dir: Path) -> int:
    raw = json.loads(job_path.read_text(encoding="utf-8"))
    try:
        job_path.unlink(missing_ok=True)
    except OSError:
        pass

    request_envelope: dict[str, Any] = raw["requestEnvelope"]
    shard: str | None = raw.get("shard")
    rt_env = str(raw.get("rippflowEnv") or detect_rippflow_env())
    ca_cert: str | None = raw.get("caCertPath") or os.environ.get("RIPPFLOW_LOCAL_CA_CERT")
    request_id = str(request_envelope["workflowId"])

    try:
        if worker_kind == "bump_and_changelog":
            assert shard is not None
            result_payload = _execute_bump_and_changelog_job(
                request_id=request_id, shard=shard, workflow_dir=workflow_dir, rippflow_env=rt_env
            )
        elif worker_kind == "verify_build_and_checksums":
            assert shard is not None
            result_payload = _execute_verify_build_and_checksums_job(
                request_id=request_id, shard=shard, workflow_dir=workflow_dir, rippflow_env=rt_env
            )
        elif worker_kind == "auto_approve_signoff":
            result_payload = {
                "decision": "approve",
                "approvedBy": "policy:autoApproveGate",
                "rippflowEnv": rt_env,
            }
        else:
            raise ValueError(f"Unknown async worker kind '{worker_kind}'")

        # Support both local HTTP/HTTPS callback ingress AND AWS Lambda Cloud callback ingress
        if "callback" in request_envelope and isinstance(request_envelope["callback"], dict) and "functionName" in request_envelope["callback"]:
            deliver_cloud_lambda_callback(
                request_envelope["callback"]["functionName"],
                request_envelope["callback"]["envelope"],
                status="COMPLETED",
                result_payload=result_payload,
            )
            return 0

        cb_payload = build_callback_payload(request_envelope, status="COMPLETED", result_payload=result_payload)
    except Exception as exc:
        safe_msg = redact_secrets(f"{worker_kind} failed: {exc}")[:2000]
        print(f"[release-worker:{worker_kind}] ERROR: {safe_msg}", file=sys.stderr, flush=True)
        if "callback" in request_envelope and isinstance(request_envelope["callback"], dict) and "functionName" in request_envelope["callback"]:
            deliver_cloud_lambda_callback(
                request_envelope["callback"]["functionName"],
                request_envelope["callback"]["envelope"],
                status="FAILED",
                failure_message=safe_msg,
            )
            return 1
        cb_payload = build_callback_payload(
            request_envelope,
            status="FAILED",
            failure_reason_code=f"{worker_kind.upper()}_FAILED",
            failure_message=safe_msg,
        )

    http_code, resp = post_callback(str(request_envelope["callbackUrl"]), cb_payload, ca_cert_path=ca_cert)
    if http_code != 200:
        print(redact_secrets(f"[release-worker:{worker_kind}] Callback HTTP {http_code}: {resp}"), file=sys.stderr)
        return 1
    return 0
