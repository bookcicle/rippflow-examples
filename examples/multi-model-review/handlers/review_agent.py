#!/usr/bin/env python3
"""Deterministic bounded multi-model build/review fixture.

Exercises slim fire-and-forget async launchers (`build`, `reviewer_a`, `reviewer_b`),
detached external callback workers with out-of-order completion across rounds,
dual-reviewer fan-in (`decision`), bounded cyclic rework (`max_loops: 9`, up to
10 rounds total), fail-closed `REWORK_LIMIT_EXHAUSTED` termination when models
never converge, and `human_gate` terminal progression on unanimous approval.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_dir_for(workflow_id: str) -> Path:
    override = os.environ.get("MULTI_MODEL_STATE_DIR")
    base = Path(override) if override else (PROJECT_ROOT / ".outputs" / "multi-model-runs")
    target = base / workflow_id
    target.mkdir(parents=True, exist_ok=True)
    return target


@contextmanager
def locked_ledger(workflow_id: str):
    rdir = run_dir_for(workflow_id)
    lock_path = rdir / ".ledger.lock"
    ledger_path = rdir / "ledger.json"
    with open(lock_path, "a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.005)
        try:
            if ledger_path.exists():
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            else:
                ledger = {
                    "workflowId": workflow_id,
                    "currentRound": 0,
                    "taskConfig": {},
                    "rounds": {},
                    "humanGate": None,
                }
            yield rdir, ledger
            tmp_path = rdir / f"ledger.{os.getpid()}.json.tmp"
            tmp_path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(ledger_path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_round_entry(ledger: dict, round_idx: int) -> dict:
    rounds = ledger.setdefault("rounds", {})
    return rounds.setdefault(
        str(round_idx),
        {
            "round": round_idx,
            "build": None,
            "pr": None,
            "reviews": {},
            "callbackOrder": [],
            "decision": None,
        },
    )


def post_authenticated_callback(request: dict, result_payload: dict) -> None:
    callback = {
        "action": "async_callback",
        "tenantId": request["tenantId"],
        "workflowId": request["workflowId"],
        "phaseId": request["phaseId"],
        "iterationId": request["iterationId"],
        "operationId": request["operationId"],
        "dispatchGeneration": request["dispatchGeneration"],
        "expectedIssuedOwnershipEpoch": request["issuedOwnershipEpoch"],
        "expectedIssuedCellId": request["issuedCellId"],
        "callbackToken": request["callbackToken"],
        "status": "COMPLETED",
        "resultPayload": result_payload,
    }
    body = json.dumps(callback).encode("utf-8")
    callback_url = request["callbackUrl"]
    ssl_ctx = None
    if callback_url.startswith("https://"):
        ca_cert = os.environ.get("RIPPFLOW_LOCAL_CA_CERT")
        if ca_cert and os.path.isfile(ca_cert):
            ssl_ctx = ssl.create_default_context(cafile=ca_cert)

    req = urllib.request.Request(
        callback_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
        assert resp.status == 200, f"unexpected callback HTTP status {resp.status}"


def run_launcher() -> None:
    request = json.load(sys.stdin)
    phase_id = request["phaseId"]
    sys.stderr.write(f"dispatching slim async launcher for {phase_id} ({request['tool']})\n")
    sys.stderr.flush()
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert child.stdin is not None
    child.stdin.write(json.dumps(request).encode("utf-8"))
    child.stdin.close()
    print(json.dumps({"outcome": "dispatched"}))


def run_worker() -> None:
    request = json.load(sys.stdin)
    workflow_id = request["workflowId"]
    phase_id = request["phaseId"]
    raw_inputs = request.get("inputs") or {}
    msg_input = raw_inputs.get("input") if isinstance(raw_inputs, dict) else {}
    if not isinstance(msg_input, dict):
        msg_input = {}

    if phase_id == "build":
        with locked_ledger(workflow_id) as (_, ledger):
            if not ledger.get("taskConfig") and msg_input:
                ledger["taskConfig"] = {
                    "feature": msg_input.get("feature", "multi-model build/review task"),
                    "targetApprovalRound": int(msg_input.get("targetApprovalRound", 2)),
                    "forceExhaustion": bool(msg_input.get("forceExhaustion", False)),
                }
            round_idx = int(ledger.get("currentRound", 0))
            entry = ensure_round_entry(ledger, round_idx)
            prior_feedback = []
            if round_idx > 0:
                prev = ledger.get("rounds", {}).get(str(round_idx - 1), {})
                for rev_name, rev_info in (prev.get("reviews") or {}).items():
                    for finding in rev_info.get("findings") or []:
                        prior_feedback.append(f"{rev_name}: {finding}")
            build_record = {
                "round": round_idx,
                "commitSha": f"commit-r{round_idx:02d}",
                "addressedFeedback": prior_feedback,
                "operationId": request["operationId"],
            }
            entry["build"] = build_record

        time.sleep(0.05)
        post_authenticated_callback(request, build_record)
        return

    if phase_id in ("reviewer_a", "reviewer_b"):
        with locked_ledger(workflow_id) as (_, ledger):
            round_idx = int(ledger.get("currentRound", 0))
            cfg = ledger.get("taskConfig") or {}
            force_exhaustion = bool(cfg.get("forceExhaustion", False))
            target_round = int(cfg.get("targetApprovalRound", 2))

        # Deliberately alternate which reviewer completes first so the fan-in
        # barrier exercises out-of-order async callbacks across rounds.
        if round_idx % 2 == 0:
            delay = 0.35 if phase_id == "reviewer_a" else 0.02
        else:
            delay = 0.02 if phase_id == "reviewer_a" else 0.35
        time.sleep(delay)

        if force_exhaustion:
            verdict = "changes_requested"
            findings = [f"{phase_id} unresolved blocker in round {round_idx}"]
        elif round_idx >= target_round:
            verdict = "approved"
            findings = []
        elif round_idx == 0:
            verdict = "changes_requested"
            findings = (
                ["add boundary test for empty shard input"]
                if phase_id == "reviewer_a"
                else ["tighten replay fingerprint assertion"]
            )
        else:
            # Odd intermediate rounds: reviewer_a approves while reviewer_b still
            # requests changes, proving both reviewers must approve before advancing.
            if phase_id == "reviewer_a":
                verdict = "approved"
                findings = []
            else:
                verdict = "changes_requested"
                findings = [f"address remaining edge case before round {target_round}"]

        review_record = {
            "reviewer": phase_id,
            "model": request["tool"],
            "round": round_idx,
            "verdict": verdict,
            "findings": findings,
            "operationId": request["operationId"],
        }
        with locked_ledger(workflow_id) as (_, ledger):
            entry = ensure_round_entry(ledger, round_idx)
            entry["reviews"][phase_id] = review_record
            entry["callbackOrder"].append(phase_id)

        post_authenticated_callback(request, review_record)
        return

    raise SystemExit(f"unsupported async worker phase: {phase_id}")


def run_sync() -> None:
    ctx = json.load(sys.stdin)
    phase = ctx["phase"]
    workflow_id = ctx["requestId"]
    loop_count = int(ctx.get("loopCount") or 0)

    if phase == "publish_pr":
        with locked_ledger(workflow_id) as (_, ledger):
            ledger["currentRound"] = loop_count
            entry = ensure_round_entry(ledger, loop_count)
            pr_record = {
                "round": loop_count,
                "prNumber": 400 + loop_count,
                "commitSha": (entry.get("build") or {}).get("commitSha", f"commit-r{loop_count:02d}"),
            }
            entry["pr"] = pr_record
        sys.stderr.write(f"published PR #{pr_record['prNumber']} for round {loop_count}\n")
        sys.stderr.flush()
        print(json.dumps({"outcome": "completed", "payload": pr_record}))
        return

    if phase == "decision":
        with locked_ledger(workflow_id) as (rdir, ledger):
            entry = ensure_round_entry(ledger, loop_count)
            reviews = entry.get("reviews") or {}
            if "reviewer_a" not in reviews or "reviewer_b" not in reviews:
                raise RuntimeError(
                    f"decision reached at round {loop_count} without both reviewer callbacks: {list(reviews.keys())}"
                )
            all_approved = (
                reviews["reviewer_a"]["verdict"] == "approved"
                and reviews["reviewer_b"]["verdict"] == "approved"
            )
            outcome = "approved" if all_approved else "changes_requested"
            decision_record = {
                "round": loop_count,
                "outcome": outcome,
                "callbackOrder": list(entry.get("callbackOrder") or []),
                "reviewerA": reviews["reviewer_a"]["verdict"],
                "reviewerB": reviews["reviewer_b"]["verdict"],
            }
            entry["decision"] = decision_record
            (rdir / f"round-{loop_count}.json").write_text(
                json.dumps(entry, indent=2) + "\n", encoding="utf-8"
            )
            if not all_approved:
                ledger["currentRound"] = loop_count + 1

        sys.stderr.write(
            f"decision round={loop_count} verdict={outcome} order={decision_record['callbackOrder']}\n"
        )
        sys.stderr.flush()
        print(json.dumps({"outcome": outcome, "payload": decision_record}))
        return

    if phase == "human_gate":
        with locked_ledger(workflow_id) as (rdir, ledger):
            orders = {
                k: v.get("callbackOrder", [])
                for k, v in sorted(ledger.get("rounds", {}).items(), key=lambda kv: int(kv[0]))
            }
            gate_summary = {
                "status": "ready_for_human_merge",
                "approvedAtRound": loop_count,
                "totalRounds": loop_count + 1,
                "callbackOrdersByRound": orders,
            }
            ledger["humanGate"] = gate_summary
            (rdir / "final-summary.json").write_text(
                json.dumps(gate_summary, indent=2) + "\n", encoding="utf-8"
            )
        sys.stderr.write(
            f"human_gate reached after {loop_count + 1} round(s); ready for human merge\n"
        )
        sys.stderr.flush()
        print(json.dumps({"outcome": "completed", "payload": gate_summary}))
        return

    raise SystemExit(f"unsupported sync phase: {phase}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if mode == "launcher":
        run_launcher()
    elif mode == "worker":
        run_worker()
    elif mode == "sync":
        run_sync()
    else:
        raise SystemExit(f"unknown review_agent mode: {mode}")


if __name__ == "__main__":
    main()
