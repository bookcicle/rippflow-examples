#!/usr/bin/env python3
"""Portable cross-platform Rippflow handler launcher.

Automatically re-executes inside project `.venv` when present (macOS, Linux, Windows)
so `handlers.json` stays 100% machine-independent and open-source friendly.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


def _maybe_reexec_in_venv(repo_root: Path) -> None:
    if os.environ.get("RIPPFLOW_PR_PIPELINE_IN_VENV") == "1":
        return

    candidates = [
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            if Path(sys.prefix).resolve() != (repo_root / ".venv").resolve():
                env = os.environ.copy()
                env["RIPPFLOW_PR_PIPELINE_IN_VENV"] = "1"
                existing_pp = env.get("PYTHONPATH", "")
                src_dir = str(repo_root / "src")
                env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else src_dir
                os.execve(str(candidate), [str(candidate), *sys.argv], env)
            break


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    _maybe_reexec_in_venv(repo_root)

    src_dir = str(repo_root / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from pr_pipeline.cli_entry import dispatch_main

    return dispatch_main(sys.argv[1:], repo_root=repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
