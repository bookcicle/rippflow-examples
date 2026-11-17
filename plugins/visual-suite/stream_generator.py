#!/usr/bin/env python3
"""
Rippflow Visual Suite Stream Generator
Generates live streaming telemetry across all visual components:
- Candlestick (OHLC latency variance)
- Histogram (token length distribution)
- ReactiveGraph (agent swarm collaboration state)
- BubbleChart (frontier model benchmarks)
- DonutChart (GPU VRAM allocation)
- GeoMap (World map cross-region latency mesh)
- Contour (2D loss landscape with topographic isolines)
- Sunburst (Hierarchical compute partition tree)
- HorizontalBar (Model throughput comparisons)
- RaceChart (Dynamic streaming leaderboard race)
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

    # 6. Geo Map (D3 World Map)
    geo_nodes = [
        {"id": "us-east", "label": "Virginia (US-East)", "x": 0.283, "y": 0.304, "status": "healthy", "ping_ms": round(18.0 + random.uniform(-1, 2), 1)},
        {"id": "us-west", "label": "Oregon (US-West)", "x": 0.165, "y": 0.261, "status": "healthy", "ping_ms": round(24.0 + random.uniform(-1, 2), 1)},
        {"id": "sa-east", "label": "São Paulo (SA)", "x": 0.371, "y": 0.750, "status": "healthy", "ping_ms": round(112.0 + random.uniform(-3, 4), 1)},
        {"id": "eu-west", "label": "Dublin (EU-West)", "x": 0.483, "y": 0.194, "status": "healthy", "ping_ms": round(72.0 + random.uniform(-2, 3), 1)},
        {"id": "eu-central", "label": "Frankfurt (EU)", "x": 0.524, "y": 0.217, "status": "healthy", "ping_ms": round(65.0 + random.uniform(-2, 3), 1)},
        {"id": "ap-se", "label": "Singapore (AP-SE)", "x": 0.788, "y": 0.570, "status": "healthy", "ping_ms": round(165.0 + random.uniform(-4, 5), 1)},
        {"id": "ap-ne", "label": "Tokyo (AP-NE)", "x": 0.888, "y": 0.321, "status": "healthy", "ping_ms": round(138.0 + random.uniform(-3, 4), 1)},
        {"id": "ap-se2", "label": "Sydney (AP-SE2)", "x": 0.920, "y": 0.825, "status": "healthy", "ping_ms": round(182.0 + random.uniform(-5, 6), 1)},
    ]
    geo_links = [
        {"from": "us-west", "to": "us-east", "latency_ms": round(42.0 + random.uniform(-2, 3), 1)},
        {"from": "us-east", "to": "eu-west", "latency_ms": round(68.0 + random.uniform(-2, 4), 1)},
        {"from": "us-east", "to": "sa-east", "latency_ms": round(95.0 + random.uniform(-3, 4), 1)},
        {"from": "eu-west", "to": "eu-central", "latency_ms": round(16.0 + random.uniform(-1, 2), 1)},
        {"from": "eu-central", "to": "ap-se", "latency_ms": round(135.0 + random.uniform(-4, 6), 1)},
        {"from": "ap-se", "to": "ap-ne", "latency_ms": round(52.0 + random.uniform(-2, 3), 1)},
        {"from": "ap-se", "to": "ap-se2", "latency_ms": round(78.0 + random.uniform(-2, 3), 1)},
    ]

    # 7. Contour Matrix (Loss Landscape)
    grid_size = 14
    contour_matrix = []
    for r in range(grid_size):
        row = []
        for c in range(grid_size):
            x = (c - 7.0) / 3.0
            y = (r - 7.0) / 3.0
            drift = math.sin(step * 0.1) * 0.2
            r2 = (x - drift) ** 2 + y ** 2
            val = round(0.12 + (r2 * 0.28) + (math.cos(x * 1.8) * 0.3) + (math.sin(y * 1.5) * 0.2), 3)
            row.append(val)
        contour_matrix.append(row)

    # 8. Sunburst tree
    sunburst_tree = {
        "name": "Compute Mesh",
        "value": 100.0,
        "children": [
            {
                "name": "Inference Engine",
                "value": round(42.0 + 3.0 * math.sin(step * 0.2), 1),
                "children": [
                    {"name": "Prefill Phase", "value": round(18.0 + math.sin(step * 0.3), 1)},
                    {"name": "Decode Phase", "value": round(24.0 + math.cos(step * 0.3), 1)}
                ]
            },
            {
                "name": "Distributed Training",
                "value": round(36.0 + 2.0 * math.cos(step * 0.2), 1),
                "children": [
                    {"name": "Forward Pass", "value": 15.0},
                    {"name": "Backward Pass", "value": 13.0},
                    {"name": "AllReduce Sync", "value": 8.0}
                ]
            },
            {
                "name": "Memory Pool",
                "value": round(22.0 + math.sin(step * 0.15), 1),
                "children": [
                    {"name": "KV Cache", "value": 14.0},
                    {"name": "Weight Buffer", "value": 8.0}
                ]
            }
        ]
    }

    # 9. Horizontal Bar items
    horizontal_items = [
        {"label": "Llama-3.1-70B", "value": round(142.5 + random.uniform(-4, 6), 1), "unit": "req/s"},
        {"label": "Claude-3.5-Sonnet", "value": round(128.0 + random.uniform(-3, 5), 1), "unit": "req/s"},
        {"label": "GPT-4o", "value": round(119.2 + random.uniform(-3, 4), 1), "unit": "req/s"},
        {"label": "DeepSeek-V3", "value": round(110.8 + random.uniform(-3, 5), 1), "unit": "req/s"},
        {"label": "Gemini-1.5-Pro", "value": round(96.4 + random.uniform(-2, 4), 1), "unit": "req/s"},
    ]

    # 10. Race Chart entries
    # Dynamically fluctuate values to trigger lead changes
    race_candidates = [
        ("1", "coder-agent-1", 94.0 + 6.0 * math.sin(step * 0.3)),
        ("2", "verifier-agent", 92.0 + 6.0 * math.cos(step * 0.3)),
        ("3", "researcher-agent", 89.0 + 5.0 * math.sin(step * 0.4 + 1.0)),
        ("4", "orchestrator", 85.0 + 4.0 * math.cos(step * 0.2)),
        ("5", "evaluator-qa", 81.0 + 3.0 * math.sin(step * 0.25)),
    ]
    race_sorted = sorted(race_candidates, key=lambda x: x[2], reverse=True)
    race_entries = []
    for rank, (cid, label, score) in enumerate(race_sorted):
        # determine simulated delta
        delta = 1 if rank == 0 and step % 4 == 0 else (-1 if rank == 1 and step % 4 == 0 else 0)
        race_entries.append({
            "id": cid,
            "label": label,
            "value": round(score, 1),
            "change": delta
        })

    # 11. US States Regional Nodes
    us_nodes = [
        {"id": "us-east-1", "label": "Virginia (US-East)", "lat": 38.0, "lon": -77.5, "status": "healthy", "ping_ms": round(11.0 + random.uniform(-1, 2), 1)},
        {"id": "us-east-2", "label": "Ohio (US-East)", "lat": 40.0, "lon": -82.9, "status": "healthy", "ping_ms": round(17.0 + random.uniform(-1, 2), 1)},
        {"id": "us-central", "label": "Iowa (Central)", "lat": 41.6, "lon": -93.6, "status": "healthy", "ping_ms": round(26.0 + random.uniform(-1, 2), 1)},
        {"id": "us-south", "label": "Texas (South)", "lat": 32.8, "lon": -96.8, "status": "healthy", "ping_ms": round(31.0 + random.uniform(-2, 3), 1)},
        {"id": "us-west-1", "label": "N. California (West)", "lat": 37.4, "lon": -121.9, "status": "healthy", "ping_ms": round(44.0 + random.uniform(-2, 3), 1)},
        {"id": "us-west-2", "label": "Oregon (West)", "lat": 45.8, "lon": -119.3, "status": "healthy", "ping_ms": round(39.0 + random.uniform(-2, 3), 1)},
    ]
    us_links = [
        {"from": "us-west-2", "to": "us-west-1", "latency_ms": round(14.0 + random.uniform(-1, 2), 1)},
        {"from": "us-west-1", "to": "us-south", "latency_ms": round(34.0 + random.uniform(-2, 3), 1)},
        {"from": "us-south", "to": "us-central", "latency_ms": round(21.0 + random.uniform(-1, 2), 1)},
        {"from": "us-central", "to": "us-east-2", "latency_ms": round(18.0 + random.uniform(-1, 2), 1)},
        {"from": "us-east-2", "to": "us-east-1", "latency_ms": round(11.0 + random.uniform(-1, 2), 1)},
    ]

    return candle, tokens, nodes, edges, bubbles, vram_segments, geo_nodes, geo_links, contour_matrix, sunburst_tree, horizontal_items, race_entries, us_nodes, us_links


def run(continuous: bool = False):
    latency_file = BASE_DIR / "latency_stream.jsonl"
    hist_file = BASE_DIR / "histogram_stream.jsonl"
    swarm_file = BASE_DIR / "swarm_mesh.json"
    bubble_file = BASE_DIR / "bubble_models.json"
    vram_file = BASE_DIR / "vram_donut.json"
    geo_file = BASE_DIR / "geo_clusters.json"
    us_geo_file = BASE_DIR / "us_states_geo.json"
    contour_file = BASE_DIR / "contour_matrix.json"
    sunburst_file = BASE_DIR / "sunburst.json"
    hbar_file = BASE_DIR / "horizontal_bar.json"
    race_file = BASE_DIR / "race_chart.json"

    # Pre-populate rolling candlesticks
    rolling_candles = []
    for s in range(16):
        c, _, _, _, _, _, _, _, _, _, _, _, _, _ = generate_samples(s)
        rolling_candles.append(c)

    step = 16
    while True:
        c, tokens, nodes, edges, bubbles, vram_segs, geo_nodes, geo_links, contour_mat, sunburst, h_items, race, u_nodes, u_links = generate_samples(step)
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
            json.dump({"nodes": geo_nodes, "links": geo_links}, f, indent=2)

        # Write US States Geo JSON
        with open(us_geo_file, "w") as f:
            json.dump({"nodes": u_nodes, "links": u_links}, f, indent=2)

        # Write Contour JSON
        with open(contour_file, "w") as f:
            json.dump({"matrix": contour_mat}, f, indent=2)

        # Write Sunburst JSON
        with open(sunburst_file, "w") as f:
            json.dump({"root": sunburst}, f, indent=2)

        # Write Horizontal Bar JSON
        with open(hbar_file, "w") as f:
            json.dump({"items": h_items}, f, indent=2)

        # Write Race Chart JSON
        with open(race_file, "w") as f:
            json.dump({"step": f"Round {step // 5 + 1} (T+{step}s)", "entries": race}, f, indent=2)

        if not continuous:
            print(f"Visual Suite snapshot generated at {BASE_DIR}")
            break

        step += 1
        time.sleep(1.0)

if __name__ == "__main__":
    is_cont = "--continuous" in sys.argv or "--stream" in sys.argv
    run(is_cont)
