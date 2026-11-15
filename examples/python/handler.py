#!/usr/bin/env python3
"""Simulated Rippflow stdin/stdout JSON handler for public local examples."""

from __future__ import annotations

import hashlib
import json
import sys


def main() -> int:
    raw = sys.stdin.read()
    try:
        context = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid handler JSON: {exc}", file=sys.stderr)
        return 1

    phase = str(context.get("phase") or "unknown")
    request_id = str(context.get("requestId") or "local-run")
    shard = str(context.get("shard") or "default")
    service = str(context.get("service") or "python-example-handler")
    loop_count = int(context.get("loopCount") or 0)
    options = context.get("options") if isinstance(context.get("options"), dict) else {}

    if shard == "fatal-crash.bin" and phase == "process-item":
        print(
            "SIMULATED_FATAL_CRASH: unrecoverable Python worker fault for shard 'fatal-crash.bin'",
            file=sys.stderr,
        )
        return 1

    outcome = "completed"
    reason = None

    if phase == "verify-balance" and shard == "stmt-feb.pdf" and loop_count == 0:
        outcome = "rework"
        reason = "balance_mismatch_low_dpi"
    elif phase == "loudness-qa" and shard == "scene-02.mp4" and loop_count == 0:
        outcome = "rework"
        reason = "loudness_outside_target"
    elif phase == "audit-item" and shard == "corrupt-item.bin":
        outcome = "rework"
        reason = "persistent_checksum_corruption"
    elif phase == "audit-item" and shard == "recoverable-item.bin" and loop_count == 0:
        outcome = "rework"
        reason = "transient_checksum_drift"

    digest = hashlib.sha256(
        f"{request_id}:{phase}:{shard}:{loop_count}:{json.dumps(options, sort_keys=True)}".encode(
            "utf-8"
        )
    ).hexdigest()

    response = {
        "outcome": outcome,
        "outputPointer": f"s3://rippflow-examples/{request_id}/{phase}-{shard}-loop{loop_count}.json",
        "payload": {
            "simulated": True,
            "runtime": "python",
            "service": service,
            "phase": phase,
            "shard": shard,
            "loopCount": loop_count,
            "reason": reason,
            "sha256": digest,
            "optionsEcho": options,
        },
    }
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
