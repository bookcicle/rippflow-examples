#!/usr/bin/env python3
"""
Simulates an execution run for pipeline-dag plugin.
Writes pipeline run status to .rippflow/pipeline/history.json.
"""
import json
import os
import sys
import time
from pathlib import Path


def main():
    workspace = Path(os.getcwd())
    pipeline_dir = workspace / ".rippflow" / "pipeline"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    history_file = pipeline_dir / "history.json"

    steps = [
        ("ingest", "Extract & Normalize Raw Parquet"),
        ("transform", "Feature Transformation & Tokenization"),
        ("train", "Distributed Model Training"),
        ("eval", "Validation & Offline Evaluation"),
        ("deploy", "Canary Rollout & Inference Smoke Test"),
    ]

    print("Starting pipeline simulation...")
    for active_idx in range(len(steps)):
        nodes = []
        for i, (node_id, label) in enumerate(steps):
            if i < active_idx:
                status = "success"
            elif i == active_idx:
                status = "running"
            else:
                status = "pending"
            nodes.append({
                "id": node_id,
                "label": label,
                "status": status,
                "step": f"{i+1:02d}",
            })

        payload = {
            "run_id": "run-pipe-20261117-01",
            "active_step": steps[active_idx][0],
            "step_index": active_idx + 1,
            "total_steps": len(steps),
            "updated_at": time.time(),
            "nodes": nodes,
        }
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        print(f"Step {active_idx+1}/{len(steps)}: {steps[active_idx][1]} ({status})")
        if "--quick" not in sys.argv:
            time.sleep(1.0)

    # Final completed state
    final_nodes = [
        {"id": nid, "label": lbl, "status": "success", "step": f"{i+1:02d}"}
        for i, (nid, lbl) in enumerate(steps)
    ]
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump({
            "run_id": "run-pipe-20261117-01",
            "status": "completed",
            "nodes": final_nodes,
        }, f, indent=2)

    print("Pipeline simulation complete!")


if __name__ == "__main__":
    main()
