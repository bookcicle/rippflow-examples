#!/usr/bin/env python3
"""
Telemetry collector for cluster-pulse plugin.
Writes memory telemetry to .rippflow/telemetry/stats.jsonl and node registry to .rippflow/telemetry/nodes.json.
"""
import json
import math
import os
import sys
import time
from pathlib import Path


def main():
    workspace = Path(os.getcwd())
    telemetry_dir = workspace / ".rippflow" / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)

    stats_file = telemetry_dir / "stats.jsonl"
    nodes_file = telemetry_dir / "nodes.json"

    # Seed nodes list
    nodes = [
        {"id": "node-local", "name": "Apple M-Series Studio (Local)", "status": "online", "ping_ms": 1, "icon": "apple-silicon"},
        {"id": "node-aws-01", "name": "AWS EC2 g5.2xlarge (us-east-1)", "status": "online", "ping_ms": 28, "icon": "nvidia"},
        {"id": "node-lambda-02", "name": "Lambda Labs 8x A100 (us-west)", "status": "online", "ping_ms": 64, "icon": "cloud-node"},
    ]
    with open(nodes_file, "w", encoding="utf-8") as f:
        json.dump(nodes, f, indent=2)

    with open(stats_file, "w", encoding="utf-8") as f:
        pass

    iterations = 5 if "--quick" in sys.argv else 20
    print(f"Collecting telemetry ({iterations} samples)...")

    for i in range(1, iterations + 1):
        now_str = time.strftime("%H:%M:%S")
        vram = round(24.0 + 8.0 * math.sin(i * 0.4), 2)
        ram = round(32.0 + 4.0 * math.cos(i * 0.3), 2)

        entry = {
            "time": now_str,
            "allocated_gb": vram,
            "system_ram_gb": ram,
            "gpu_temp_c": 58 + (i % 6),
            "timestamp": time.time(),
        }
        with open(stats_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        print(f"[{now_str}] Sample {i:02d} - VRAM: {vram} GB - RAM: {ram} GB")
        if "--quick" not in sys.argv:
            time.sleep(1.0)

    print("Telemetry collection complete!")


if __name__ == "__main__":
    main()
