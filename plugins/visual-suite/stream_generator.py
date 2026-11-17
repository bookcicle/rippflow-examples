#!/usr/bin/env python3
"""
Rippflow Visual Suite Stream Generator
Generates live streaming telemetry across all 9 visual components:
- Candlestick (OHLC latency variance)
- Histogram (token length samples)
- ReactiveGraph (agent swarm collaboration state)
- BubbleChart (frontier model benchmarks)
- DonutChart (GPU VRAM allocation)
- GeoMap (cross-region latency mesh)
- Contour (2D loss landscape)
"""

import sys
import os
import time
import math
import json
import random
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()

def generate_samples(step: int):
    # 1. Candlestick sample
    base = 45.0 + 8.0 * math.sin(step * 0.15)
    c_open = round(base + random.uniform(-2.0, 2.0), 2)
    c_close = round(c_open + random.uniform(-4.0, 4.5), 2)
    c_high = round(max(c_open, c_close) + random.uniform(1.0, 5.0), 2)
    c_low = round(max(5.0, min(c_open, c_close) - random.uniform(1.0, 4.0)), 2)
    candle = {
        "step": f"T+{step}",
        "time": f"14:{step:02d}",
        "open": c_open,
        "high": c_high,
        "low": c_low,
        "close": c_close
    }

    # 2. Histogram sample (token distribution)
    tokens = [
        int(random.gauss(128 + 20 * math.sin(step * 0.1), 32))
        for _ in range(30)
    ]

    # 3. Reactive Swarm Graph
    roles = ["active", "working", "idle"]
    nodes = [
        {"id": "coord", "label": "Coordinator", "status": "active", "role": "Orchestrator", "x": 0.50, "y": 0.22},
        {"id": "res", "label": "Researcher", "status": random.choice(roles), "role": "Ingest", "x": 0.18, "y": 0.65},
        {"id": "code", "label": "Coder", "status": random.choice(roles), "role": "CodeGen", "x": 0.50, "y": 0.78},
        {"id": "ver", "label": "Verifier", "status": random.choice(roles), "role": "QA Test", "x": 0.82, "y": 0.65},
    ]
    edges = [
        {"from": "coord", "to": "res", "label": f"{random.randint(10, 50)} msg/s"},
        {"from": "res", "to": "code", "label": f"{random.randint(20, 80)} ctx/s"},
        {"from": "code", "to": "ver", "label": f"{random.randint(5, 30)} patch/s"},
        {"from": "ver", "to": "coord", "label": "verdict"},
    ]

    # 4. Bubble chart
    bubbles = [
        {"label": "gpt-4o", "x": 88.0 + random.uniform(-4, 4), "y": 0.89, "r": 18.0},
        {"label": "claude-3.5", "x": 72.0 + random.uniform(-3, 3), "y": 0.92, "r": 22.0},
        {"label": "gemini-1.5", "x": 60.0 + random.uniform(-3, 3), "y": 0.87, "r": 16.0},
        {"label": "llama-3-70b", "x": 36.0 + random.uniform(-2, 2), "y": 0.82, "r": 14.0},
        {"label": "deepseek-v2", "x": 26.0 + random.uniform(-2, 2), "y": 0.85, "r": 15.0},
    ]

    # 5. Donut VRAM
    vram_kv = round(9.0 + 2.0 * math.sin(step * 0.2), 1)
    vram_act = round(3.5 + 0.8 * math.cos(step * 0.25), 1)
    vram_free = round(max(0.5, 24.0 - 8.2 - vram_kv - vram_act), 1)
    vram_segments = [
        {"label": "KV Cache", "value": vram_kv},
        {"label": "Model Weights", "value": 8.2},
        {"label": "Activations", "value": vram_act},
        {"label": "Free Capacity", "value": vram_free},
    ]

    # 6. Geo Map Links
    geo_links = [
        {"from": "us-east", "to": "us-west", "latency_ms": round(38.0 + random.uniform(-2, 4), 1)},
        {"from": "us-east", "to": "eu-west", "latency_ms": round(66.0 + random.uniform(-3, 5), 1)},
        {"from": "eu-west", "to": "ap-se", "latency_ms": round(138.0 + random.uniform(-5, 8), 1)},
    ]

    return candle, tokens, nodes, edges, bubbles, vram_segments, geo_links


def run(continuous: bool = False):
    latency_file = BASE_DIR / "latency_stream.jsonl"
    hist_file = BASE_DIR / "histogram_stream.jsonl"
    swarm_file = BASE_DIR / "swarm_mesh.json"
    bubble_file = BASE_DIR / "bubble_models.json"
    vram_file = BASE_DIR / "vram_donut.json"
    geo_file = BASE_DIR / "geo_clusters.json"

    # Pre-populate rolling candlesticks
    rolling_candles = []
    for s in range(16):
        c, _, _, _, _, _, _ = generate_samples(s)
        rolling_candles.append(c)

    step = 16
    while True:
        c, tokens, nodes, edges, bubbles, vram_segs, geo_links = generate_samples(step)
        rolling_candles.append(c)
        if len(rolling_candles) > 20:
            rolling_candles.pop(0)

        # Write latency JSONL
        with open(latency_file, "w") as f:
            for item in rolling_candles:
                f.write(json.dumps(item) + "\n")

        # Write histogram JSONL
        with open(hist_file, "w") as f:
            for val in tokens:
                f.write(json.dumps({"value": val}) + "\n")

        # Write swarm JSON
        with open(swarm_file, "w") as f:
            json.dump({"nodes": nodes, "edges": edges}, f, indent=2)

        # Write bubbles JSON
        with open(bubble_file, "w") as f:
            json.dump(bubbles, f, indent=2)

        # Write VRAM donut JSON
        with open(vram_file, "w") as f:
            json.dump({"segments": vram_segs}, f, indent=2)

        # Write Geo Map JSON
        with open(geo_file, "w") as f:
            json.dump({"links": geo_links}, f, indent=2)

        if not continuous:
            print(f"Visual Suite snapshot generated at {BASE_DIR}")
            break

        step += 1
        time.sleep(1.0)

if __name__ == "__main__":
    is_cont = "--continuous" in sys.argv or "--stream" in sys.argv
    run(is_cont)
