#!/usr/bin/env python3
"""Rippflow handler entrypoint for `rippflow-nightwatch` (`local` + `cloud`)."""

from __future__ import annotations

import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = (
    REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt"
    else REPO_ROOT / ".venv" / "bin" / "python"
)

if VENV_PYTHON.is_file() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from nightwatch.cli_entry import dispatch_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(dispatch_main(sys.argv[1:], workflow_dir=REPO_ROOT))
