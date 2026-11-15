#!/usr/bin/env python3
"""Slim local adapter: hand work to another process, acknowledge, and exit."""
import json
from pathlib import Path
import subprocess
import sys

request = json.load(sys.stdin)
child = subprocess.Popen(
    [sys.executable, str(Path(__file__).with_name("worker.py"))],
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,  # Never keep the launcher's outcome pipe open.
    stderr=subprocess.DEVNULL,  # Production adapters can use an independent log sink.
)
child.stdin.write(json.dumps(request).encode())
child.stdin.close()
print(json.dumps({"outcome": "dispatched"}))
