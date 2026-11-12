"""Rippflow stdin/stdout JSON dispatcher, AWS Lambda cloud entrypoint, and worktree cleanup utility."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .callback_client import deliver_cloud_lambda_callback, redact_secrets
from .models import NightwatchRunStore, detect_rippflow_env
from .phases import (
    _execute_audit_job,
    _execute_benchmark_job,
    _execute_upgrade_job,
    handle_audit_deps_and_warnings_async,
    handle_benchmark_regression_check_async,
    handle_bind_shard,
    handle_morning_digest_and_notify,
    handle_reclaim_disk_and_worktrees,
    handle_upgrade_and_fix_breaking_apis_async,
    run_detached_async_worker,
)


MAX_STDIN_BYTES = 1_048_576


def _apply_fallback_tier_arg(extra_args: list[str]) -> None:
    for arg in extra_args:
        if arg.startswith("--tier="):
            tier = arg.split("=", 1)[1].strip().lower()
            if tier in {"local", "device", "edge", "cloud"} and not (
                os.environ.get("RIPPFLOW_ENV") or os.environ.get("rippflow_env")
            ):
                os.environ["RIPPFLOW_ENV"] = tier


def dispatch_phase_envelope(phase_name: str, envelope: dict[str, Any], *, workflow_dir: Path) -> dict[str, Any]:
    if phase_name == "reclaim_disk_and_worktrees":
        return handle_reclaim_disk_and_worktrees(envelope, workflow_dir)
    if phase_name == "bind_shard":
        return handle_bind_shard(envelope, workflow_dir)
    if phase_name == "audit_deps_and_warnings":
        return handle_audit_deps_and_warnings_async(envelope, workflow_dir)
    if phase_name == "upgrade_and_fix_breaking_apis":
        return handle_upgrade_and_fix_breaking_apis_async(envelope, workflow_dir)
    if phase_name == "benchmark_regression_check":
        return handle_benchmark_regression_check_async(envelope, workflow_dir)
    if phase_name == "morning_digest_and_notify":
        return handle_morning_digest_and_notify(envelope, workflow_dir)
    raise ValueError(f"Unknown handler phase '{phase_name}'")


def dispatch_main(argv: list[str], *, workflow_dir: Path) -> int:
    if not argv:
        print(
            "Usage: dispatch.py <phase> [--tier=local|cloud] | dispatch.py --async-worker <kind> <job_path>",
            file=sys.stderr,
        )
        return 2

    if argv[0] == "--async-worker":
        if len(argv) < 3:
            print("Usage: dispatch.py --async-worker <kind> <job_path>", file=sys.stderr)
            return 2
        return run_detached_async_worker(argv[1], Path(argv[2]), workflow_dir)

    if argv[0] == "--cleanup-run":
        parser = argparse.ArgumentParser()
        parser.add_argument("--cleanup-run", action="store_true")
        parser.add_argument("--run-id", required=True)
        args = parser.parse_args(argv)
        store = NightwatchRunStore(workflow_dir, args.run_id, rippflow_env=detect_rippflow_env())
        for st in store.load_all_shards():
            if Path(st.worktree_path).exists():
                subprocess.run(
                    ["git", "-C", st.repo_path, "worktree", "remove", "--force", st.worktree_path],
                    check=False,
                )
            subprocess.run(["git", "-C", st.repo_path, "worktree", "prune"], check=False)
        shutil.rmtree(store.worktrees_dir, ignore_errors=True)
        print(json.dumps({"ok": True, "cleanedRunId": args.run_id}))
        return 0

    phase_name = argv[0].strip()
    _apply_fallback_tier_arg(argv[1:])

    raw_bytes = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw_bytes) > MAX_STDIN_BYTES:
        print("Handler input exceeds 1 MiB limit", file=sys.stderr)
        return 1

    try:
        envelope: dict[str, Any] = json.loads(raw_bytes.decode("utf-8"))
        out = dispatch_phase_envelope(phase_name, envelope, workflow_dir=workflow_dir)
    except Exception as exc:
        print(redact_secrets(f"[{phase_name}] ERROR: {exc}"), file=sys.stderr)
        return 1

    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


def lambda_handler(event: dict[str, Any], _context: Any = None, *, workflow_dir: Path | None = None) -> dict[str, Any]:
    """AWS Cloud Lambda entrypoint supporting both inline slices and `aws_lambda` Event async adapters."""
    os.environ.setdefault("RIPPFLOW_ENV", "cloud")
    root = workflow_dir or Path(__file__).resolve().parents[2]

    callback_block = event.get("callback")
    if isinstance(callback_block, dict) and "functionName" in callback_block and "envelope" in callback_block:
        cb_fn = str(callback_block["functionName"])
        cb_env = callback_block["envelope"]
        phase_id = str(event.get("phaseId") or cb_env.get("phaseId") or "")
        request_id = str(event.get("workflowId") or cb_env.get("workflowId") or "")
        inputs = event.get("inputs") or {}
        inp = inputs.get("input") if isinstance(inputs, dict) else {}
        shard = str((inp or {}).get("shard") or event.get("shard") or "")

        try:
            if phase_id == "audit_deps_and_warnings":
                res = _execute_audit_job(request_id=request_id, shard=shard, workflow_dir=root, rippflow_env="cloud")
            elif phase_id == "upgrade_and_fix_breaking_apis":
                res = _execute_upgrade_job(request_id=request_id, shard=shard, workflow_dir=root, rippflow_env="cloud")
            elif phase_id == "benchmark_regression_check":
                res = _execute_benchmark_job(request_id=request_id, shard=shard, workflow_dir=root, rippflow_env="cloud")
            else:
                raise ValueError(f"Unsupported cloud async adapter phase '{phase_id}'")
            return deliver_cloud_lambda_callback(cb_fn, cb_env, status="COMPLETED", result_payload=res)
        except Exception as exc:
            deliver_cloud_lambda_callback(cb_fn, cb_env, status="FAILED", failure_message=redact_secrets(str(exc))[:1500])
            raise

    phase_name = str(event.get("phase") or event.get("service") or os.environ.get("RIPPFLOW_PHASE_NAME") or "")
    if not phase_name:
        raise ValueError("Cloud Lambda event missing 'phase' / 'service'")
    return dispatch_phase_envelope(phase_name, event, workflow_dir=root)
