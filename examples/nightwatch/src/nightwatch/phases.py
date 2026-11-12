"""Phase implementations for `rippflow-nightwatch` (`local` + `cloud`).

Phases:
1. `reclaim_disk_and_worktrees` (root, inline, 60s):
   Prunes stale git worktrees, cleans Rust Cargo `target/` directories to reclaim disk space,
   prunes unused Docker images/builders, and creates isolated per-shard worktrees.
2. `bind_shard` (per_shard, inline, 30s):
   Binds shard context into payload for downstream `async_tool` workers.
3. `audit_deps_and_warnings` (per_shard, `type: async_tool`, 30s launcher + 30m callback):
   Scans security advisories (`cargo audit` / `npm audit`), outdated dependencies (`cargo outdated` / `pip` / `npm`),
   compiler/clippy warnings, and runs multi-pass flaky test detection.
4. `upgrade_and_fix_breaking_apis` (per_shard, `type: async_tool`, 30s launcher + 60m callback):
   Upgrades dependencies in the isolated worktree (deterministic in `cloud`, optional Gemini/Codex breaking-API
   call-site fixer in `local`), commits changes, and optionally opens/stages ready-to-merge green PRs.
5. `benchmark_regression_check` (per_shard, `type: async_tool`, 30s launcher + 30m callback):
   Compares baseline vs candidate latency/throughput benchmarks, verifies test suite green status,
   and opens/records ready-to-merge PR links when green.
6. `morning_digest_and_notify` (fan_in_all, terminal, inline, 60s):
   Aggregates reclaimed GBs, advisories, flaky tests, benchmark deltas, and ready-to-merge green PR links
   into `MORNING_DIGEST.md` + `digest.json`, firing native OS notifications (`local`) + Slack/email webhooks.
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

from .auditor import (
    detect_ecosystem,
    detect_flaky_tests,
    scan_compiler_and_linter_warnings,
    scan_outdated_dependencies,
    scan_security_advisories,
)
from .callback_client import (
    build_callback_payload,
    deliver_cloud_lambda_callback,
    post_callback,
    redact_secrets,
)
from .hygiene import execute_hygiene_sweep, format_bytes
from .models import (
    NightwatchConfig,
    NightwatchRepoSpec,
    NightwatchRunStore,
    ShardNightwatchState,
    detect_rippflow_env,
    is_cloud_env,
)
from .notify import MorningDigestAlert, dispatch_morning_digest_notifications
from .upgrader import upgrade_and_adapt_worktree


_BENCH_MS_RE = re.compile(r"(?:p99|latency|mean|bench_ms|duration_ms)\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:ms)?", re.IGNORECASE)


@contextmanager
def _repo_git_lock(src_repo: Path) -> Iterator[None]:
    lock_dir = Path(tempfile.gettempdir()) / "rippflow-nightwatch-git-locks"
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


def _create_isolated_worktree(spec: NightwatchRepoSpec, dest: Path, branch_name: str, git_user: str, git_email: str) -> None:
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
        _git(dest, ["config", "user.name", git_user], check=False)
        _git(dest, ["config", "user.email", git_email], check=False)
        _git(dest, ["config", "commit.gpgsign", "false"], check=False)


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
    shard: str,
) -> dict[str, Any]:
    request_id = str(request_envelope["workflowId"])
    operation_id = str(request_envelope["operationId"])
    rt_env = detect_rippflow_env()
    store = NightwatchRunStore(workflow_dir, request_id, rippflow_env=rt_env)
    jobs_dir = store.run_dir / "async_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    safe_op = re.sub(r"[^a-zA-Z0-9_.-]", "_", operation_id)
    job_path = jobs_dir / f"{worker_kind}-{shard}-{safe_op}.job.json"
    log_path = jobs_dir / f"{worker_kind}-{shard}-{safe_op}.log"

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


def _parse_benchmark_ms(output: str) -> float | None:
    try:
        obj = json.loads(output.strip())
        if isinstance(obj, dict) and "latencyMs" in obj:
            return float(obj["latencyMs"])
    except Exception:
        pass
    m = _BENCH_MS_RE.search(output)
    if m:
        return float(m.group(1))
    return None


def _run_benchmark_probe(cmd: list[str], *, cwd: Path, env: dict[str, str], role: str) -> tuple[float | None, str]:
    run_env = env.copy()
    run_env["NIGHTWATCH_BENCH_ROLE"] = role
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=run_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"Benchmark command failed ({role}): {combined[:400]}")
    parsed = _parse_benchmark_ms(combined)
    return (parsed if parsed is not None else round(elapsed_ms, 2)), combined[:300]


# ---------------------------------------------------------------------------
# Phase 1: reclaim_disk_and_worktrees (root, inline)
# ---------------------------------------------------------------------------
def handle_reclaim_disk_and_worktrees(ctx: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    request_id = str(ctx["requestId"])
    raw_input = ctx.get("payload") or {}
    if not isinstance(raw_input, dict):
        raise ValueError("Input payload must be a JSON object")

    cfg = NightwatchConfig.from_raw(raw_input, workflow_dir=workflow_dir)
    store = NightwatchRunStore(workflow_dir, request_id, rippflow_env=cfg.rippflow_env)
    store.save_config(cfg)

    # Step 1: Execute workspace hygiene sweep BEFORE creating fresh isolated upgrade worktrees!
    hygiene_report = execute_hygiene_sweep(
        repos=cfg.repos,
        options=cfg.hygiene,
        rippflow_env=cfg.rippflow_env,
        execution_mode=cfg.execution_mode,
    )
    store.save_hygiene_report(hygiene_report)

    # Step 2: Create fresh isolated git worktrees for every configured repo
    short_req = request_id.split("-")[0]
    git_user = cfg.gh_user or "rippflow-nightwatch-bot"
    git_email = cfg.git_email or "nightwatch-bot@rippflow.local"

    for name, spec in cfg.repos.items():
        wt_path = store.worktrees_dir / name
        branch_name = f"{cfg.branch_prefix}/{name}-{short_req}"
        _create_isolated_worktree(spec, wt_path, branch_name, git_user, git_email)
        sha = _head_sha(wt_path)
        eco = detect_ecosystem(wt_path)
        store.save_shard(
            ShardNightwatchState(
                shard=name,
                repo_path=spec.path,
                worktree_path=str(wt_path),
                branch_name=branch_name,
                base_branch=spec.base_branch,
                ecosystem=eco,
                head_sha_before=sha,
                head_sha_after=sha,
            )
        )

    return {
        "outcome": "completed",
        "payload": {
            "requestId": request_id,
            "rippflowEnv": cfg.rippflow_env,
            "aiUpgraderEnabled": cfg.ai_upgrader_enabled,
            "hygieneReport": hygiene_report,
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

    store = NightwatchRunStore(workflow_dir, request_id)
    cfg = store.load_config()
    if not store.shard_file(shard).is_file():
        spec = cfg.repos.get(shard) or NightwatchRepoSpec(name=shard, path=str((Path(cfg.workspace_dir) / shard).resolve()))
        cfg.repos[shard] = spec
        store.save_config(cfg)
        wt_path = store.worktrees_dir / shard
        branch_name = f"{cfg.branch_prefix}/{shard}-{request_id.split('-')[0]}"
        _create_isolated_worktree(
            spec,
            wt_path,
            branch_name,
            cfg.gh_user or "rippflow-nightwatch-bot",
            cfg.git_email or "nightwatch-bot@rippflow.local",
        )
        sha = _head_sha(wt_path)
        store.save_shard(
            ShardNightwatchState(
                shard=shard,
                repo_path=spec.path,
                worktree_path=str(wt_path),
                branch_name=branch_name,
                base_branch=spec.base_branch,
                ecosystem=detect_ecosystem(wt_path),
                head_sha_before=sha,
                head_sha_after=sha,
            )
        )

    state = store.load_shard(shard)
    return {
        "outcome": "completed",
        "payload": {
            "requestId": request_id,
            "shard": shard,
            "ecosystem": state.ecosystem,
            "rippflowEnv": cfg.rippflow_env,
        },
    }


# ---------------------------------------------------------------------------
# Phase 3: audit_deps_and_warnings (per_shard, async_tool)
# ---------------------------------------------------------------------------
def handle_audit_deps_and_warnings_async(request_envelope: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "audit_deps_and_warnings")
    return _spawn_detached_worker(
        worker_kind="audit_deps_and_warnings",
        request_envelope=request_envelope,
        workflow_dir=workflow_dir,
        shard=shard,
    )


def _execute_audit_job(*, request_id: str, shard: str, workflow_dir: Path, rippflow_env: str) -> dict[str, Any]:
    store = NightwatchRunStore(workflow_dir, request_id, rippflow_env=rippflow_env)
    cfg = store.load_config()
    state = store.load_shard(shard)
    spec = cfg.repos[shard]
    wt = Path(state.worktree_path)

    env = os.environ.copy()
    if state.ecosystem == "rust":
        env["CARGO_TARGET_DIR"] = str(wt.parent / f".cargo-target-{shard}")

    advisories = scan_security_advisories(wt, ecosystem=state.ecosystem, explicit_cmd=spec.audit_command, env=env)
    outdated = scan_outdated_dependencies(wt, ecosystem=state.ecosystem, explicit_cmd=spec.outdated_command, env=env)
    warn_count, warn_sample = scan_compiler_and_linter_warnings(
        wt, ecosystem=state.ecosystem, explicit_cmd=spec.warnings_command, env=env
    )
    suite_ok, flaky_tests = detect_flaky_tests(
        wt,
        ecosystem=state.ecosystem,
        explicit_cmd=spec.test_command,
        runs=cfg.flaky_test_runs,
        env=env,
    )

    # Capture baseline benchmark latency BEFORE applying dependency upgrades!
    if spec.benchmark_command:
        base_ms, _ = _run_benchmark_probe(spec.benchmark_command, cwd=wt, env=env, role="baseline")
        state.baseline_bench_ms = base_ms

    state.advisories = advisories
    state.outdated_packages = outdated
    state.compiler_warnings_count = warn_count
    state.compiler_warnings_sample = warn_sample
    state.flaky_tests = flaky_tests
    state.test_suite_passed = suite_ok
    state.status = "audited"
    store.save_shard(state)

    return {
        "shard": shard,
        "ecosystem": state.ecosystem,
        "advisoriesCount": len(advisories),
        "outdatedCount": len(outdated),
        "compilerWarningsCount": warn_count,
        "flakyTests": flaky_tests,
        "testSuitePassed": suite_ok,
    }


# ---------------------------------------------------------------------------
# Phase 4: upgrade_and_fix_breaking_apis (per_shard, async_tool)
# ---------------------------------------------------------------------------
def handle_upgrade_and_fix_breaking_apis_async(request_envelope: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "upgrade_and_fix_breaking_apis")
    return _spawn_detached_worker(
        worker_kind="upgrade_and_fix_breaking_apis",
        request_envelope=request_envelope,
        workflow_dir=workflow_dir,
        shard=shard,
    )


def _execute_upgrade_job(*, request_id: str, shard: str, workflow_dir: Path, rippflow_env: str) -> dict[str, Any]:
    store = NightwatchRunStore(workflow_dir, request_id, rippflow_env=rippflow_env)
    cfg = store.load_config()
    state = store.load_shard(shard)
    spec = cfg.repos[shard]
    wt = Path(state.worktree_path)

    env = os.environ.copy()
    if state.ecosystem == "rust":
        env["CARGO_TARGET_DIR"] = str(wt.parent / f".cargo-target-{shard}")

    effective_ai = False if is_cloud_env(rippflow_env) else cfg.ai_upgrader_enabled
    modified_files, upgrader_used, summary = upgrade_and_adapt_worktree(
        wt=wt,
        ecosystem=state.ecosystem,
        outdated_packages=state.outdated_packages,
        advisories=state.advisories,
        compiler_warnings_sample=state.compiler_warnings_sample,
        explicit_upgrade_cmd=spec.upgrade_command,
        verify_test_cmd=spec.test_command,
        rippflow_env=rippflow_env,
        ai_enabled=effective_ai,
        provider=cfg.upgrader_provider,
        gemini_model=cfg.gemini_model,
        codex_model=cfg.codex_model,
        env=env,
    )

    if modified_files:
        _git(wt, ["add", "-A"])
        _git(wt, ["commit", "-m", f"chore(deps): nightwatch automated dependency hygiene for {shard}"])

    state.upgraded_files = modified_files
    state.upgrader_used = upgrader_used
    state.upgrade_summary = summary
    state.head_sha_after = _head_sha(wt)
    state.status = "upgraded"
    store.save_shard(state)

    return {
        "shard": shard,
        "upgraderUsed": upgrader_used,
        "aiUsed": upgrader_used.startswith("local-ai:"),
        "upgradedFiles": modified_files,
        "headShaAfter": state.head_sha_after,
    }


# ---------------------------------------------------------------------------
# Phase 5: benchmark_regression_check (per_shard, async_tool)
# ---------------------------------------------------------------------------
def handle_benchmark_regression_check_async(request_envelope: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    shard = _extract_async_shard(request_envelope, "benchmark_regression_check")
    return _spawn_detached_worker(
        worker_kind="benchmark_regression_check",
        request_envelope=request_envelope,
        workflow_dir=workflow_dir,
        shard=shard,
    )


def _maybe_open_or_stage_pr(cfg: NightwatchConfig, state: ShardNightwatchState, wt: Path) -> str:
    if cfg.execution_mode == "live" and shutil.which("gh"):
        remote_probe = _git(wt, ["remote", "get-url", "origin"], check=False)
        if remote_probe.returncode == 0 and remote_probe.stdout.strip():
            _git(wt, ["push", "-u", "origin", state.branch_name], check=False)
            pr_proc = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    f"chore(deps): Nightwatch automated upgrade ({state.shard})",
                    "--body",
                    f"Automated overnight dependency & warning hygiene pass.\n\n- Upgrader: `{state.upgrader_used}`\n- Benchmark: `{state.benchmark_summary}`",
                    "--base",
                    state.base_branch,
                    "--head",
                    state.branch_name,
                ],
                cwd=str(wt),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if pr_proc.returncode == 0 and pr_proc.stdout.strip().startswith("http"):
                return pr_proc.stdout.strip().splitlines()[-1]
    return f"local-branch://{state.shard}/{state.branch_name}@{state.head_sha_after[:7]}"


def _execute_benchmark_job(*, request_id: str, shard: str, workflow_dir: Path, rippflow_env: str) -> dict[str, Any]:
    store = NightwatchRunStore(workflow_dir, request_id, rippflow_env=rippflow_env)
    cfg = store.load_config()
    state = store.load_shard(shard)
    spec = cfg.repos[shard]
    wt = Path(state.worktree_path)

    env = os.environ.copy()
    if state.ecosystem == "rust":
        env["CARGO_TARGET_DIR"] = str(wt.parent / f".cargo-target-{shard}")

    # Re-verify test suite on upgraded commit
    post_test_ok = True
    if spec.test_command:
        tproc = subprocess.run(
            spec.test_command,
            cwd=str(wt),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        post_test_ok = tproc.returncode == 0
    state.test_suite_passed = post_test_ok

    bench_ok = True
    bench_summary = "skipped"
    if spec.benchmark_command:
        cand_ms, raw_out = _run_benchmark_probe(spec.benchmark_command, cwd=wt, env=env, role="candidate")
        state.candidate_bench_ms = cand_ms
        base_ms = state.baseline_bench_ms
        if base_ms and base_ms > 0 and cand_ms is not None:
            delta_pct = round(((cand_ms - base_ms) / base_ms) * 100.0, 2)
            state.benchmark_delta_pct = delta_pct
            bench_ok = delta_pct <= cfg.max_benchmark_regression_pct
            bench_summary = (
                f"baseline={base_ms:.2f}ms -> candidate={cand_ms:.2f}ms ({delta_pct:+.2f}%, "
                f"limit={cfg.max_benchmark_regression_pct:.1f}%)"
            )
        else:
            bench_summary = raw_out or f"candidate={cand_ms}ms"

    # Reclaim temporary worktree `.cargo-target-<shard>` immediately after verification so overnight runs stay disk-neutral!
    temp_cargo_dir = wt.parent / f".cargo-target-{shard}"
    if temp_cargo_dir.is_dir():
        shutil.rmtree(temp_cargo_dir, ignore_errors=True)

    has_upgrade_commit = state.head_sha_after != state.head_sha_before
    is_green = post_test_ok and bench_ok
    state.benchmark_passed = bench_ok
    state.benchmark_summary = bench_summary
    state.green_ready_to_merge = bool(has_upgrade_commit and is_green)
    if state.green_ready_to_merge:
        state.pr_url = _maybe_open_or_stage_pr(cfg, state, wt)
    state.status = "benchmarked"
    store.save_shard(state)

    return {
        "shard": shard,
        "testSuitePassed": post_test_ok,
        "benchmarkPassed": bench_ok,
        "benchmarkSummary": bench_summary,
        "benchmarkDeltaPct": state.benchmark_delta_pct,
        "greenReadyToMerge": state.green_ready_to_merge,
        "prUrl": state.pr_url,
    }


# ---------------------------------------------------------------------------
# Phase 6: morning_digest_and_notify (fan_in_all, terminal, inline)
# ---------------------------------------------------------------------------
def handle_morning_digest_and_notify(ctx: dict[str, Any], workflow_dir: Path) -> dict[str, Any]:
    request_id = str(ctx["requestId"])
    rt_env = detect_rippflow_env()
    store = NightwatchRunStore(workflow_dir, request_id, rippflow_env=rt_env)
    cfg = store.load_config()
    hygiene_report = store.load_hygiene_report()
    shards = store.load_all_shards()

    reclaimed_bytes = int(hygiene_report.get("totalBytesReclaimed", 0))
    reclaimed_human = hygiene_report.get("totalHumanReclaimed") or format_bytes(reclaimed_bytes)

    green_prs: list[str] = []
    flagged_shards: list[str] = []
    total_advisories = 0
    total_flaky = 0

    md_lines = [
        "# Rippflow Nightwatch — Morning Repo Hygiene & Upgrade Digest",
        "",
        f"- **Run ID:** `{request_id}` (`RIPPFLOW_ENV={rt_env}`, `mode={cfg.execution_mode}`)",
        f"- **Disk Space Reclaimed:** **{reclaimed_human}** (`{reclaimed_bytes}` bytes)",
        f"  - Stale git worktrees cleaned: `{len((hygiene_report.get('worktrees') or {}).get('removedWorktrees', []))}`",
        f"  - Cargo `target/` dirs cleaned: `{len((hygiene_report.get('cargoTargets') or {}).get('cleanedTargets', []))}`",
        f"  - Docker image/builder prune: `{(hygiene_report.get('docker') or {}).get('status', 'n/a')}`",
        "",
        "## Repository Shards",
        "",
    ]

    shard_summaries: list[dict[str, Any]] = []
    for st in shards:
        total_advisories += len(st.advisories)
        total_flaky += len(st.flaky_tests)
        if st.green_ready_to_merge and st.pr_url:
            green_prs.append(st.pr_url)
        if (not st.test_suite_passed) or (not st.benchmark_passed) or st.advisories or st.flaky_tests:
            flagged_shards.append(st.shard)

        badge = "GREEN READY TO MERGE" if st.green_ready_to_merge else (
            "REGRESSION / ATTENTION NEEDED" if (not st.test_suite_passed or not st.benchmark_passed) else "CLEAN (NO UPGRADES NEEDED)"
        )
        md_lines.extend(
            [
                f"### `{st.shard}` — {badge}",
                f"- **Ecosystem:** `{st.ecosystem}` · **Upgrader:** `{st.upgrader_used}`",
                f"- **Security Advisories:** `{len(st.advisories)}` · **Outdated Packages:** `{len(st.outdated_packages)}` · **Compiler Warnings:** `{st.compiler_warnings_count}`",
                f"- **Flaky Tests Detected:** `{', '.join(st.flaky_tests) if st.flaky_tests else 'none'}`",
                f"- **Benchmark Check:** `{st.benchmark_summary}` (`passed={st.benchmark_passed}`)",
                f"- **Green PR / Branch:** `{st.pr_url or 'n/a'}`",
                "",
            ]
        )
        shard_summaries.append(
            {
                "shard": st.shard,
                "ecosystem": st.ecosystem,
                "advisoriesCount": len(st.advisories),
                "outdatedCount": len(st.outdated_packages),
                "compilerWarningsCount": st.compiler_warnings_count,
                "flakyTests": st.flaky_tests,
                "upgradedFiles": st.upgraded_files,
                "upgraderUsed": st.upgrader_used,
                "testSuitePassed": st.test_suite_passed,
                "benchmarkPassed": st.benchmark_passed,
                "benchmarkDeltaPct": st.benchmark_delta_pct,
                "benchmarkSummary": st.benchmark_summary,
                "greenReadyToMerge": st.green_ready_to_merge,
                "prUrl": st.pr_url,
            }
        )

    digest_md = "\n".join(md_lines).rstrip() + "\n"
    (store.run_dir / "MORNING_DIGEST.md").write_text(digest_md, encoding="utf-8")

    alert = MorningDigestAlert(
        title="Rippflow Nightwatch Morning Digest",
        subtitle=f"Reclaimed {reclaimed_human} · {len(green_prs)} green PR(s) ready",
        body=(
            f"Shards: {len(shards)} | Green PRs: {len(green_prs)} | "
            f"Advisories: {total_advisories} | Flaky tests: {total_flaky}"
        ),
        run_id=request_id,
        reclaimed_human=reclaimed_human,
        green_prs=green_prs,
        flagged_shards=flagged_shards,
    )
    receipts = dispatch_morning_digest_notifications(
        alert,
        rippflow_env=rt_env,
        enable_os_notifications=cfg.enable_os_notifications,
        slack_webhook_url=cfg.slack_webhook_url,
        email_webhook_url=cfg.email_webhook_url,
        alert_log_path=store.run_dir / "notifications.jsonl",
    )

    digest_payload = {
        "schemaVersion": 1,
        "requestId": request_id,
        "rippflowEnv": rt_env,
        "executionMode": cfg.execution_mode,
        "aiUpgraderEnabled": False if is_cloud_env(rt_env) else cfg.ai_upgrader_enabled,
        "reclaimedBytes": reclaimed_bytes,
        "reclaimedHuman": reclaimed_human,
        "greenPrLinks": green_prs,
        "flaggedShards": flagged_shards,
        "shards": shard_summaries,
        "notificationReceipts": [r.to_dict() for r in receipts],
    }
    (store.run_dir / "digest.json").write_text(json.dumps(digest_payload, indent=2), encoding="utf-8")

    return {"outcome": "completed", "payload": digest_payload}


# ---------------------------------------------------------------------------
# Detached worker & Cloud AWS Lambda adapter execution
# ---------------------------------------------------------------------------
def run_detached_async_worker(worker_kind: str, job_path: Path, workflow_dir: Path) -> int:
    raw = json.loads(job_path.read_text(encoding="utf-8"))
    try:
        job_path.unlink(missing_ok=True)
    except OSError:
        pass

    request_envelope: dict[str, Any] = raw["requestEnvelope"]
    shard = str(raw["shard"])
    rt_env = str(raw.get("rippflowEnv") or detect_rippflow_env())
    ca_cert: str | None = raw.get("caCertPath") or os.environ.get("RIPPFLOW_LOCAL_CA_CERT")
    request_id = str(request_envelope["workflowId"])

    try:
        if worker_kind == "audit_deps_and_warnings":
            result_payload = _execute_audit_job(
                request_id=request_id, shard=shard, workflow_dir=workflow_dir, rippflow_env=rt_env
            )
        elif worker_kind == "upgrade_and_fix_breaking_apis":
            result_payload = _execute_upgrade_job(
                request_id=request_id, shard=shard, workflow_dir=workflow_dir, rippflow_env=rt_env
            )
        elif worker_kind == "benchmark_regression_check":
            result_payload = _execute_benchmark_job(
                request_id=request_id, shard=shard, workflow_dir=workflow_dir, rippflow_env=rt_env
            )
        else:
            raise ValueError(f"Unknown nightwatch worker kind '{worker_kind}'")

        if isinstance(request_envelope.get("callback"), dict) and "functionName" in request_envelope["callback"]:
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
        print(f"[nightwatch-worker:{worker_kind}] ERROR: {safe_msg}", file=sys.stderr, flush=True)
        if isinstance(request_envelope.get("callback"), dict) and "functionName" in request_envelope["callback"]:
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
        print(redact_secrets(f"[nightwatch-worker:{worker_kind}] Callback HTTP {http_code}: {resp}"), file=sys.stderr)
        return 1
    return 0
