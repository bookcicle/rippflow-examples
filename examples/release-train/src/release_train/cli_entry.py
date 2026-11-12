"""Rippflow stdin/stdout JSON protocol dispatcher, AWS Lambda cloud entrypoint, and human release sign-off CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .callback_client import (
    build_callback_payload,
    deliver_cloud_lambda_callback,
    post_callback,
    redact_secrets,
)
from .models import ReleaseRunStore, detect_rippflow_env
from .phases import (
    _execute_bump_and_changelog_job,
    _execute_verify_build_and_checksums_job,
    handle_bind_shard,
    handle_bump_and_changelog_async,
    handle_detect_drift,
    handle_publish_and_sync_docs,
    handle_release_signoff_gate_async,
    handle_verify_build_and_checksums_async,
    run_detached_async_worker,
)


MAX_STDIN_BYTES = 1_048_576


def _apply_fallback_tier_arg(extra_args: list[str]) -> None:
    """Applies `--tier=local|cloud` ONLY if `RIPPFLOW_ENV` / `rippflow_env` is not already injected by Rippflow."""
    for arg in extra_args:
        if arg.startswith("--tier="):
            tier = arg.split("=", 1)[1].strip().lower()
            if tier in {"local", "device", "edge", "cloud"} and not (
                os.environ.get("RIPPFLOW_ENV") or os.environ.get("rippflow_env")
            ):
                os.environ["RIPPFLOW_ENV"] = tier


def dispatch_phase_envelope(phase_name: str, envelope: dict[str, Any], *, workflow_dir: Path) -> dict[str, Any]:
    if phase_name == "detect_drift":
        return handle_detect_drift(envelope, workflow_dir)
    if phase_name == "bind_shard":
        return handle_bind_shard(envelope, workflow_dir)
    if phase_name == "bump_and_changelog":
        return handle_bump_and_changelog_async(envelope, workflow_dir)
    if phase_name == "verify_build_and_checksums":
        return handle_verify_build_and_checksums_async(envelope, workflow_dir)
    if phase_name == "release_signoff_gate":
        return handle_release_signoff_gate_async(envelope, workflow_dir)
    if phase_name == "publish_and_sync_docs":
        return handle_publish_and_sync_docs(envelope, workflow_dir)
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
    """AWS Cloud Lambda entrypoint supporting both direct Rippflow phase slices and `aws_lambda` Event async adapters.

    In deployed Rippflow Cloud cells (`RIPPFLOW_ENV=cloud`), Codex and Gemini are flagged off automatically
    and all changelogs, builds, notifications, and callbacks execute with zero AI CLI/SDK dependencies.
    """
    os.environ.setdefault("RIPPFLOW_ENV", "cloud")
    root = workflow_dir or Path(__file__).resolve().parents[2]

    # Case 1: Deployed `aws_lambda` async_tool adapter invocation containing `callback: { functionName, envelope }`
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
            if phase_id == "bump_and_changelog":
                res = _execute_bump_and_changelog_job(
                    request_id=request_id, shard=shard, workflow_dir=root, rippflow_env="cloud"
                )
                return deliver_cloud_lambda_callback(cb_fn, cb_env, status="COMPLETED", result_payload=res)
            if phase_id == "verify_build_and_checksums":
                res = _execute_verify_build_and_checksums_job(
                    request_id=request_id, shard=shard, workflow_dir=root, rippflow_env="cloud"
                )
                return deliver_cloud_lambda_callback(cb_fn, cb_env, status="COMPLETED", result_payload=res)
            if phase_id == "release_signoff_gate":
                # Dispatches Slack/email webhooks and leaves workflow suspended until human callback!
                handle_release_signoff_gate_async(event, root)
                store = ReleaseRunStore(root, request_id, rippflow_env="cloud")
                cfg = store.load_config()
                if cfg.auto_approve_gate:
                    return deliver_cloud_lambda_callback(
                        cb_fn,
                        cb_env,
                        status="COMPLETED",
                        result_payload={"decision": "approve", "approvedBy": "policy:autoApproveGate", "rippflowEnv": "cloud"},
                    )
                return {"status": "awaiting_human_signoff", "workflowId": request_id}
            raise ValueError(f"Unsupported cloud async adapter phase '{phase_id}'")
        except Exception as exc:
            safe_err = redact_secrets(str(exc))[:1500]
            deliver_cloud_lambda_callback(cb_fn, cb_env, status="FAILED", failure_message=safe_err)
            raise

    # Case 2: Inline synchronous Lambda phase invocation (`detect_drift`, `bind_shard`, `publish_and_sync_docs`)
    phase_name = str(event.get("phase") or event.get("service") or os.environ.get("RIPPFLOW_PHASE_NAME") or "")
    if not phase_name:
        raise ValueError("Cloud Lambda event missing 'phase' / 'service'")
    return dispatch_phase_envelope(phase_name, event, workflow_dir=root)


def _discover_pending_signoff_files(workflow_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for base in (
        workflow_dir / ".runs",
        Path(os.environ.get("RIPPFLOW_STATE_DIR") or "/tmp/rippflow-release-train/.runs"),
    ):
        if base.exists():
            for path in sorted(base.glob("*/signoff/release_signoff.callback.json")):
                if path not in candidates:
                    candidates.append(path)
    return candidates


def resolve_release_gate_cli(argv: list[str], *, workflow_dir: Path) -> int:
    parser = argparse.ArgumentParser(
        description="List, approve, or reject suspended Rippflow Multi-Repo Release Train sign-off gates."
    )
    parser.add_argument("--list", action="store_true", help="List pending release sign-off gates")
    parser.add_argument("--run-id", help="Workflow request/run ID (optional if only one pending gate exists)")
    parser.add_argument(
        "--decision",
        choices=["approve", "reject"],
        help="Approve (`COMPLETED` -> proceeds to ordered tag/publish) or reject (`FAILED` -> aborts release)",
    )
    parser.add_argument("--approver", default="release-manager", help="Approver identifier recorded in audit payload")
    parser.add_argument("--note", default="", help="Optional release sign-off note")
    parser.add_argument("--cleanup-worktrees", action="store_true", help="Remove isolated release git worktrees for --run-id")
    args = parser.parse_args(argv)

    if args.cleanup_worktrees:
        if not args.run_id:
            print("--cleanup-worktrees requires --run-id", file=sys.stderr)
            return 2
        store = ReleaseRunStore(workflow_dir, args.run_id, rippflow_env=detect_rippflow_env())
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

    pending_files = _discover_pending_signoff_files(workflow_dir)

    if args.list or not args.decision:
        gates = []
        for pf in pending_files:
            rec = json.loads(pf.read_text(encoding="utf-8"))
            meta = rec.get("metadata") or {}
            gates.append(
                {
                    "runId": rec["requestEnvelope"]["workflowId"],
                    "releaseVersion": meta.get("releaseVersion"),
                    "rippflowEnv": meta.get("rippflowEnv"),
                    "shards": meta.get("shards", []),
                    "publishOrder": meta.get("publishOrder", []),
                    "tokenFingerprint": rec.get("tokenFingerprint"),
                    "resolveCommand": meta.get("resolveCommand"),
                }
            )
        print(json.dumps({"pendingReleaseGates": gates}, indent=2))
        return 0

    matched: Path | None = None
    for pf in pending_files:
        rec = json.loads(pf.read_text(encoding="utf-8"))
        wf_id = rec["requestEnvelope"]["workflowId"]
        if args.run_id and wf_id != args.run_id:
            continue
        matched = pf
        break

    if matched is None:
        print("No matching pending release sign-off gate found. Run with `--list` to inspect active gates.", file=sys.stderr)
        return 1

    rec = json.loads(matched.read_text(encoding="utf-8"))
    req_env = rec["requestEnvelope"]
    wf_id = str(req_env["workflowId"])

    if args.decision == "approve":
        result_payload = {
            "decision": "approve",
            "approvedBy": args.approver,
            "note": args.note or "Approved for ordered release tagging and docs sync.",
        }
        if isinstance(req_env.get("callback"), dict) and "functionName" in req_env["callback"]:
            cb_resp = deliver_cloud_lambda_callback(
                req_env["callback"]["functionName"],
                req_env["callback"]["envelope"],
                status="COMPLETED",
                result_payload=result_payload,
            )
            http_status = 200
        else:
            payload = build_callback_payload(req_env, status="COMPLETED", result_payload=result_payload)
            http_status, cb_resp = post_callback(
                str(rec.get("callbackUrl") or req_env["callbackUrl"]),
                payload,
                ca_cert_path=rec.get("caCertPath"),
            )
    else:
        reason_msg = args.note or f"Release sign-off rejected by {args.approver}."
        if isinstance(req_env.get("callback"), dict) and "functionName" in req_env["callback"]:
            cb_resp = deliver_cloud_lambda_callback(
                req_env["callback"]["functionName"],
                req_env["callback"]["envelope"],
                status="FAILED",
                failure_message=reason_msg,
            )
            http_status = 200
        else:
            payload = build_callback_payload(
                req_env,
                status="FAILED",
                failure_reason_code="RELEASE_SIGNOFF_REJECTED",
                failure_message=reason_msg,
            )
            http_status, cb_resp = post_callback(
                str(rec.get("callbackUrl") or req_env["callbackUrl"]),
                payload,
                ca_cert_path=rec.get("caCertPath"),
            )

    if http_status != 200:
        print(json.dumps({"ok": False, "httpStatus": http_status, "response": cb_resp}, indent=2), file=sys.stderr)
        return 1

    matched.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "ok": True,
                "runId": wf_id,
                "decision": args.decision,
                "approvedBy": args.approver,
                "httpStatus": http_status,
                "callbackResponse": cb_resp,
            },
            indent=2,
        )
    )
    return 0
