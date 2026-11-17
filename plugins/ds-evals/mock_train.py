#!/usr/bin/env python3
"""
Simulates a training run for ds-evals plugin.
Writes metrics to .rippflow/runs/latest/metrics.jsonl and checkpoints to .rippflow/checkpoints/index.json.
"""
import json
import math
import os
import sys
import time
from pathlib import Path


def main():
    workspace = Path(os.getcwd())
    runs_dir = workspace / ".rippflow" / "runs" / "latest"
    ckpt_dir = workspace / ".rippflow" / "checkpoints"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    metrics_file = runs_dir / "metrics.jsonl"
    ckpt_file = ckpt_dir / "index.json"

    # Reset metrics file
    with open(metrics_file, "w", encoding="utf-8") as f:
        pass

    checkpoints = []
    total_epochs = 10 if "--quick" in sys.argv else 30

    print(f"Starting training simulation ({total_epochs} epochs)...")
    for epoch in range(1, total_epochs + 1):
        train_loss = round(0.8 * math.exp(-0.08 * epoch) + 0.05, 4)
        val_loss = round(0.85 * math.exp(-0.075 * epoch) + 0.07, 4)
        accuracy = round(1.0 - val_loss, 4)

        entry = {
            "epoch": epoch,
            "train_loss": train_loss,
            "val_loss": val_loss,
            "accuracy": accuracy,
            "timestamp": time.time(),
        }
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        if epoch % 5 == 0 or epoch == total_epochs:
            ckpt_info = {
                "id": f"checkpoint-epoch-{epoch}",
                "name": f"model_epoch_{epoch:03d}.pt",
                "epoch": epoch,
                "val_loss": val_loss,
                "size_mb": 142.5,
                "is_best": (epoch == total_epochs),
            }
            checkpoints.append(ckpt_info)
            with open(ckpt_file, "w", encoding="utf-8") as f:
                json.dump(checkpoints, f, indent=2)

        print(f"Epoch {epoch:02d}/{total_epochs:02d} - train_loss: {train_loss} - val_loss: {val_loss}")
        if "--quick" not in sys.argv:
            time.sleep(0.5)

    print("Training simulation complete!")


if __name__ == "__main__":
    main()
