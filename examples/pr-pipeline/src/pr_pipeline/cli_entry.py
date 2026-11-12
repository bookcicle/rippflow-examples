"""Rippflow stdin/stdout JSON protocol entrypoint and human escalation resolver."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .callback_client import build_callback_payload, post_callback, redact_secrets
from .models import RunStore
from .phases import (
    handle_author_pr_async,
    handle_bind_shard,
    handle_codex_review_async,
    handle_human_gate_async,
    handle_local_test_suite_async,
    handle_prepare_workspace,
    handle_publish_summary,
    handle_route_review_outcome,
    handle_route_test_outcome,
    run_detached_async_worker,
)


MAX_STDIN_BYTES = 1_048_576


def dispatch_main(argv: list[str], *, repo_root: Path) -> int:
    if not argv:
        print("Usage: dispatch.py <phase> | dispatch.py --async-worker <kind> <job_path>", file=sys.stderr)
        return 2

    if argv[0] == "--async-worker":
        if len(argv) < 3:
            print("Usage: dispatch.py --async-worker <kind> <job_path>", file=sys.stderr)
            return 2
        return run_detached_async_worker(argv[1], Path(argv[2]), repo_root)

    phase_name = argv[0].strip()
    raw_bytes = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw_bytes) > MAX_STDIN_BYTES:
        print("Handler input exceeds 1 MiB limit", file=sys.stderr)
        return 1

    try:
        envelope: dict[str, Any] = json.loads(raw_bytes.decode("utf-8"))
        if phase_name == "prepare_workspace":
            out = handle_prepare_workspace(envelope, repo_root)
        elif phase_name == "bind_shard":
            out = handle_bind_shard(envelope, repo_root)
        elif phase_name == "author_pr":
            out = handle_author_pr_async(envelope, repo_root)
        elif phase_name == "local_test_suite":
            out = handle_local_test_suite_async(envelope, repo_root)
        elif phase_name == "route_test_outcome":
            out = handle_route_test_outcome(envelope, repo_root)
        elif phase_name == "codex_review":
            out = handle_codex_review_async(envelope, repo_root)
        elif phase_name == "route_review_outcome":
            out = handle_route_review_outcome(envelope, repo_root)
        elif phase_name == "human_gate":
            out = handle_human_gate_async(envelope, repo_root)
        elif phase_name == "publish_summary":
            out = handle_publish_summary(envelope, repo_root)
        else:
            raise ValueError(f"Unknown handler phase '{phase_name}'")
    except Exception as exc:
        print(redact_secrets(f"[{phase_name}] ERROR: {exc}"), file=sys.stderr)
        return 1

    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


def resolve_gate_cli(argv: list[str], *, repo_root: Path) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect and resolve paused 15-turn human review escalations via Rippflow's authenticated callback."
    )
    parser.add_argument("--list", action="store_true", help="List all pending human escalations")
    parser.add_argument("--run-id", help="Workflow request/run ID (optional if only one pending escalation exists)")
    parser.add_argument("--shard", help="Repository shard name")
    parser.add_argument(
        "--decision",
        choices=["approve", "reject"],
        help="Resume workflow (`approve` -> COMPLETED) or fail workflow (`reject` -> FAILED)",
    )
    parser.add_argument("--note", default="", help="Optional human review note recorded in the callback payload")
    parser.add_argument("--cleanup-worktrees", action="store_true", help="Clean up run git worktrees for --run-id")
    args = parser.parse_args(argv)

    runs_root = repo_root / ".runs"
    pending_files = sorted(runs_root.glob("*/escalations/*.callback.json")) if runs_root.exists() else []

    if args.cleanup_worktrees:
        if not args.run_id:
            print("--cleanup-worktrees requires --run-id", file=sys.stderr)
            return 2
        store = RunStore(repo_root, args.run_id)
        cfg_file = store.run_dir / "config.json"
        comp_sources: list[tuple[Path, Path]] = []
        if cfg_file.is_file():
            cfg = store.load_config()
            for spec in cfg.repos.values():
                for comp in spec.companion_repos:
                    comp_name = Path(comp).name
                    comp_src = Path(comp) if Path(comp).is_absolute() else (Path(cfg.workspace_dir) / comp).resolve()
                    comp_sources.append((comp_src, store.worktrees_dir / comp_name))
        for s in store.load_all_shards():
            if Path(s.worktree_path).exists():
                subprocess.run(
                    ["git", "-C", s.repo_path, "worktree", "remove", "--force", s.worktree_path],
                    check=False,
                )
            subprocess.run(["git", "-C", s.repo_path, "worktree", "prune"], check=False)
        for comp_src, comp_wt in comp_sources:
            if comp_wt.exists():
                subprocess.run(
                    ["git", "-C", str(comp_src), "worktree", "remove", "--force", str(comp_wt)],
                    check=False,
                )
            subprocess.run(["git", "-C", str(comp_src), "worktree", "prune"], check=False)
        shutil.rmtree(store.worktrees_dir, ignore_errors=True)
        print(json.dumps({"ok": True, "cleanedRunId": args.run_id}))
        return 0

    if args.list or not args.decision:
        items = []
        for pf in pending_files:
            rec = json.loads(pf.read_text(encoding="utf-8"))
            items.append(
                {
                    "runId": rec["requestEnvelope"]["workflowId"],
                    "shard": rec["metadata"]["shard"],
                    "turnsUsed": rec["metadata"]["turnsUsed"],
                    "prUrl": rec["metadata"].get("prUrl"),
                    "reason": rec["metadata"]["escalationReason"],
                    "tokenFingerprint": rec["tokenFingerprint"],
                    "resolveCommand": rec["metadata"]["resolveCommand"],
                }
            )
        print(json.dumps({"pendingEscalations": items}, indent=2))
        return 0

    matched: Path | None = None
    for pf in pending_files:
        rec = json.loads(pf.read_text(encoding="utf-8"))
        wf_id = rec["requestEnvelope"]["workflowId"]
        sh_name = rec["metadata"]["shard"]
        if args.run_id and wf_id != args.run_id:
            continue
        if args.shard and sh_name != args.shard:
            continue
        matched = pf
        break

    if matched is None:
        print("No matching pending escalation found. Run with `--list` to inspect active escalations.", file=sys.stderr)
        return 1

    rec = json.loads(matched.read_text(encoding="utf-8"))
    req_env = rec["requestEnvelope"]
    shard_name = rec["metadata"]["shard"]
    wf_id = req_env["workflowId"]

    if args.decision == "approve":
        payload = build_callback_payload(
            req_env,
            status="COMPLETED",
            result_payload={
                "shard": shard_name,
                "resolution": "human_approved",
                "note": args.note or "Approved manually after human escalation review.",
            },
        )
    else:
        payload = build_callback_payload(
            req_env,
            status="FAILED",
            failure_reason_code="HUMAN_REVIEW_REJECTED",
            failure_message=args.note or f"Human reviewer rejected escalated PR for shard '{shard_name}'.",
        )

    status_code, resp = post_callback(
        rec["callbackUrl"],
        payload,
        ca_cert_path=rec.get("caCertPath"),
    )
    if status_code != 200:
        print(json.dumps({"ok": False, "httpStatus": status_code, "response": resp}, indent=2), file=sys.stderr)
        return 1

    store = RunStore(repo_root, wf_id)
    state = store.load_shard(shard_name)
    state.status = "human_approved" if args.decision == "approve" else "human_rejected"
    state.human_resolution = {
        "decision": args.decision,
        "note": args.note,
        "httpStatus": status_code,
    }
    store.save_shard(state)
    matched.unlink(missing_ok=True)

    print(
        json.dumps(
            {
                "ok": True,
                "runId": wf_id,
                "shard": shard_name,
                "decision": args.decision,
                "httpStatus": status_code,
                "callbackResponse": resp,
            },
            indent=2,
        )
    )
    return 0
