#!/usr/bin/env python3
"""Runs `rippflow dev --stream-events` while streaming live event & profile telemetry for Rippflow Studio / `rippflow ui`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any


PHASES_META = [
    ("prepare_workspace", 60, "python-standard"),
    ("bind_shard", 30, "python-light"),
    ("author_pr", 30, "python-light"),
    ("local_test_suite", 30, "python-light"),
    ("route_test_outcome", 30, "python-light"),
    ("codex_review", 30, "python-light"),
    ("route_review_outcome", 30, "python-light"),
    ("human_gate", 30, "python-light"),
    ("publish_summary", 30, "python-light"),
]


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _initial_live_profile() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    phases = []
    for name, timeout_secs, group_name in PHASES_META:
        phases.append(
            {
                "phase": name,
                "service": name,
                "runtime": "Python",
                "runtimeGroup": group_name,
                "runtimeGroupSource": "workflow_default" if group_name == "python-light" else "phase_override",
                "workerMemoryMib": 1024 if group_name == "python-light" else 2048,
                "handlerSliceMib": 256 if group_name == "python-light" else 512,
                "availableHandlerMemoryMib": 512 if group_name == "python-light" else 1024,
                "peakHandlerMemoryMib": 0,
                "aggregateProcessTreeMemoryMib": 0,
                "maxDescendantProcesses": 0,
                "utilizationPct": 0.0,
                "executionTimeMs": 0,
                "maxInvocationTimeMs": 0,
                "groupTimeoutSecs": timeout_secs,
                "phaseTimeoutSecs": timeout_secs,
                "effectiveTimeoutSecs": timeout_secs,
                "timeUtilizationPct": 0.0,
                "invocations": 0,
                "warmDaemonReuse": False,
                "resourceLimitViolations": [],
                "recommendedRuntimeGroup": group_name,
                "requiresExternalCompute": False,
                "externalComputePattern": None,
                "recommendationReason": "Pending execution",
                "memoryStatus": "PASS",
                "timeStatus": "PASS",
                "status": "PENDING",
                "enforcementMechanism": "local timeout watchdog",
                "measurementEnvironment": "local_os_sampled",
                "memoryMeasurementAvailable": True,
                "executionStatus": "PENDING",
                "callbackRejectionCount": 0,
            }
        )
    return {
        "schemaVersion": 1,
        "workflow": "multi-repo-feature-pr-pipeline",
        "workflowVersion": "1.1.0",
        "recordedAtEpochSecs": now_ms // 1000,
        "recordedAtEpochMs": now_ms,
        "measurementEnvironment": "local_os_sampled (live in-flight telemetry)",
        "awsLambdaMeasurement": False,
        "memoryStatus": "PASS",
        "timeStatus": "PASS",
        "overallStatus": "RUNNING",
        "executionStatus": "RUNNING",
        "phases": phases,
    }


def _apply_event_to_profile(
    profile: dict[str, Any],
    ev_obj: dict[str, Any],
    phase_starts: dict[str, int],
) -> None:
    now_ms = int(time.time() * 1000)
    profile["recordedAtEpochSecs"] = now_ms // 1000
    profile["recordedAtEpochMs"] = now_ms
    ev_name = ev_obj.get("event")
    phase_name = ev_obj.get("phase")
    ts = int(ev_obj.get("timestampMs") or now_ms)

    phase_entry = next((p for p in profile["phases"] if p["phase"] == phase_name), None)
    if ev_name == "handler_started" and phase_entry is not None:
        phase_starts[phase_name] = ts
        phase_entry["invocations"] = int(phase_entry["invocations"]) + 1
        phase_entry["status"] = "RUNNING"
        phase_entry["executionStatus"] = "RUNNING"
        phase_entry["recommendationReason"] = (
            f"Running invocation #{phase_entry['invocations']} (shard={ev_obj.get('shard') or 'root'})"
        )
    elif ev_name == "handler_completed" and phase_entry is not None:
        dur = max(1, ts - phase_starts.get(phase_name, ts))
        phase_entry["executionTimeMs"] += dur
        phase_entry["maxInvocationTimeMs"] = max(int(phase_entry["maxInvocationTimeMs"]), dur)
        eff_ms = max(1, int(phase_entry["effectiveTimeoutSecs"]) * 1000)
        phase_entry["timeUtilizationPct"] = round((phase_entry["maxInvocationTimeMs"] / eff_ms) * 100.0, 1)
        outcome = str(ev_obj.get("outcome") or "completed")
        phase_entry["status"] = "REWORK" if outcome == "rework" else "PASS"
        phase_entry["executionStatus"] = "COMPLETED"
        phase_entry["recommendationReason"] = f"Last outcome: {outcome} ({dur / 1000.0:.2f}s)"
    elif ev_name in {"async_tool_suspended", "async_tool_waiting_for_callback"} and phase_entry is not None:
        phase_starts[phase_name] = ts
        phase_entry["invocations"] = int(phase_entry["invocations"]) + 1
        phase_entry["status"] = "WAITING CALLBACK"
        phase_entry["executionStatus"] = "WAITING_FOR_CALLBACK"
        phase_entry["callbackStatus"] = "WAITING"
        phase_entry["recommendationReason"] = (
            f"Worker released; awaiting authenticated callback (shard={ev_obj.get('shard') or 'root'}, loop={ev_obj.get('loop', 0)})"
        )
    elif ev_name == "async_tool_resumed" and phase_entry is not None:
        wait_ms = int(ev_obj.get("callbackWaitMs") or max(1, ts - phase_starts.get(phase_name, ts)))
        phase_entry["callbackWaitTimeMs"] = int(phase_entry.get("callbackWaitTimeMs") or 0) + wait_ms
        phase_entry["callbackStatus"] = "COMPLETED"
        phase_entry["status"] = "PASS"
        phase_entry["executionStatus"] = "COMPLETED"
        phase_entry["recommendationReason"] = f"Resumed via HMAC callback (external wait {wait_ms / 1000.0:.1f}s)"
    elif ev_name == "handler_failed" and phase_entry is not None:
        err = str(ev_obj.get("error") or "handler failed")
        phase_entry["status"] = "FAIL"
        phase_entry["executionStatus"] = "FAILED"
        phase_entry["failureReason"] = err
        profile["overallStatus"] = "FAIL"
        profile["executionStatus"] = "FAILED"
        profile["failureReason"] = err
    elif ev_name in {"terminal", "workflow_completed"}:
        profile["overallStatus"] = "PASS"
        profile["executionStatus"] = "COMPLETED"


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute workflow with live TUI/Studio event streaming.")
    parser.add_argument("--rippflow-bin", default=os.environ.get("RIPPFLOW_BIN") or shutil.which("rippflow") or "rippflow")
    parser.add_argument("--workflow", default="workflow.yaml")
    parser.add_argument("--handlers", default="handlers.json")
    parser.add_argument("--input", default="input.local.json")
    parser.add_argument("--shards", required=True, help="Comma-separated shard list")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--report", default=".runs/live-report.json")
    parser.add_argument("--profile-output", default=".runs/live-profile.json")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    report_path = (repo_root / args.report).resolve()
    profile_path = (repo_root / args.profile_output).resolve()
    profile_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    venv_bin = repo_root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"

    state_lock = threading.Lock()
    report_doc: dict[str, Any] = {
        "schemaVersion": 1,
        "workflow": "multi-repo-feature-pr-pipeline",
        "workflowVersion": "1.0.0",
        "requestId": None,
        "status": "running",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "events": [],
    }
    live_profile = _initial_live_profile()
    phase_starts: dict[str, int] = {}
    _atomic_write_json(report_path, report_doc)
    _atomic_write_json(profile_path, live_profile)

    cmd = [
        args.rippflow_bin,
        "dev",
        args.workflow,
        "--handlers",
        args.handlers,
        "--shards",
        args.shards,
        "--input",
        args.input,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--profile",
        "--output",
        str(profile_path),
        "--stream-events",
    ]
    print(f"[run_monitored] Starting: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def _pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    stderr_thread = threading.Thread(target=_pump_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        sys.stdout.write(raw_line)
        sys.stdout.flush()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            with state_lock:
                if "event" in parsed:
                    report_doc["events"].append(parsed)
                    if report_doc["requestId"] is None and parsed.get("runId"):
                        report_doc["requestId"] = parsed["runId"]
                    _apply_event_to_profile(live_profile, parsed, phase_starts)
                    _atomic_write_json(profile_path, live_profile)
                    ev = parsed["event"]
                    if ev == "workflow_completed" or (isinstance(ev, dict) and ev.get("type") == "WorkflowCompleted"):
                        report_doc["status"] = "completed"
                    elif ev in ("workflow_failed", "handler_failed") or (isinstance(ev, dict) and ev.get("type") == "WorkflowFailed"):
                        report_doc["status"] = "failed"
                elif "events" in parsed and "workflow" in parsed:
                    report_doc.update(parsed)
                _atomic_write_json(report_path, report_doc)

    rc = proc.wait()
    stderr_thread.join(timeout=2.0)
    with state_lock:
        if report_doc["status"] == "running":
            report_doc["status"] = "completed" if rc == 0 else "failed"
        report_doc["completedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report_doc["exitCode"] = rc
        _atomic_write_json(report_path, report_doc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
