#!/usr/bin/env python3
"""CLI helper to inspect or resolve paused 15-turn human review gates."""

from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    src_dir = str(repo_root / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from pr_pipeline.cli_entry import resolve_gate_cli

    return resolve_gate_cli(sys.argv[1:], repo_root=repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
