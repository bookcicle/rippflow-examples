"""Per-shard dependency security auditing, outdated package detection, compiler/clippy warning
counting, and multi-pass flaky test detection across Rust, Python, and Node repositories.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


def detect_ecosystem(repo_dir: Path) -> str:
    if (repo_dir / "Cargo.toml").is_file():
        return "rust"
    if (repo_dir / "pyproject.toml").is_file() or (repo_dir / "requirements.txt").is_file():
        return "python"
    if (repo_dir / "package.json").is_file():
        return "node"
    return "generic"


def _run_tool(cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def scan_security_advisories(
    wt: Path,
    *,
    ecosystem: str,
    explicit_cmd: list[str] | None,
    env: dict[str, str],
) -> list[dict[str, str]]:
    cmd = explicit_cmd
    if cmd is None:
        if ecosystem == "rust" and shutil.which("cargo-audit"):
            cmd = ["cargo", "audit", "--json"]
        elif ecosystem == "node" and shutil.which("npm") and (wt / "package-lock.json").is_file():
            cmd = ["npm", "audit", "--json"]

    if not cmd:
        return []

    proc = _run_tool(cmd, cwd=wt, env=env, timeout=180)
    raw = (proc.stdout or "").strip()
    advisories: list[dict[str, str]] = []
    if not raw:
        return advisories

    try:
        parsed = json.loads(raw)
        # Rust `cargo audit --json` format
        vulns = ((parsed.get("vulnerabilities") or {}).get("list")) if isinstance(parsed, dict) else None
        if isinstance(vulns, list):
            for v in vulns[:25]:
                adv = v.get("advisory") or {}
                pkg = v.get("package") or {}
                advisories.append(
                    {
                        "id": str(adv.get("id") or "RUSTSEC"),
                        "package": str(pkg.get("name") or "unknown"),
                        "version": str(pkg.get("version") or ""),
                        "title": str(adv.get("title") or "Security advisory"),
                    }
                )
            return advisories

        # Custom JSON list `[{"id": ..., "package": ..., "title": ...}]`
        if isinstance(parsed, list):
            for item in parsed[:25]:
                if isinstance(item, dict):
                    advisories.append(
                        {
                            "id": str(item.get("id") or "ADVISORY"),
                            "package": str(item.get("package") or "unknown"),
                            "version": str(item.get("version") or ""),
                            "title": str(item.get("title") or "Advisory"),
                        }
                    )
            return advisories
    except json.JSONDecodeError:
        for ln in raw.splitlines()[:10]:
            if "RUSTSEC-" in ln or "CVE-" in ln or "CRITICAL" in ln.upper():
                advisories.append({"id": "ADVISORY", "package": "repo", "version": "", "title": ln.strip()[:200]})
    return advisories


def scan_outdated_dependencies(
    wt: Path,
    *,
    ecosystem: str,
    explicit_cmd: list[str] | None,
    env: dict[str, str],
) -> list[dict[str, str]]:
    cmd = explicit_cmd
    if cmd is None:
        if ecosystem == "rust" and shutil.which("cargo-outdated"):
            cmd = ["cargo", "outdated", "--format", "json", "--root-deps-only"]
        elif ecosystem == "python":
            cmd = [sys.executable, "-m", "pip", "list", "--outdated", "--format=json"]
        elif ecosystem == "node" and shutil.which("npm"):
            cmd = ["npm", "outdated", "--json"]

    if not cmd:
        return []

    proc = _run_tool(cmd, cwd=wt, env=env, timeout=180)
    raw = (proc.stdout or "").strip()
    if not raw:
        return []

    outdated: list[dict[str, str]] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            # `pip list --outdated --format=json` or custom JSON array
            for entry in parsed[:30]:
                if isinstance(entry, dict):
                    outdated.append(
                        {
                            "name": str(entry.get("name") or entry.get("package") or ""),
                            "current": str(entry.get("version") or entry.get("current") or ""),
                            "latest": str(entry.get("latest_version") or entry.get("latest") or ""),
                        }
                    )
        elif isinstance(parsed, dict):
            # `cargo outdated --format json` -> `{"dependencies": [...]}` or `npm outdated --json` -> `{"pkg": {...}}`
            deps = parsed.get("dependencies")
            if isinstance(deps, list):
                for d in deps[:30]:
                    if isinstance(d, dict):
                        outdated.append(
                            {
                                "name": str(d.get("name") or ""),
                                "current": str(d.get("project") or ""),
                                "latest": str(d.get("latest") or d.get("compat") or ""),
                            }
                        )
            else:
                for pkg_name, meta in list(parsed.items())[:30]:
                    if isinstance(meta, dict):
                        outdated.append(
                            {
                                "name": str(pkg_name),
                                "current": str(meta.get("current") or ""),
                                "latest": str(meta.get("latest") or meta.get("wanted") or ""),
                            }
                        )
    except json.JSONDecodeError:
        pass
    return [o for o in outdated if o.get("name")]


def scan_compiler_and_linter_warnings(
    wt: Path,
    *,
    ecosystem: str,
    explicit_cmd: list[str] | None,
    env: dict[str, str],
) -> tuple[int, list[str]]:
    cmd = explicit_cmd
    if cmd is None:
        if ecosystem == "rust" and shutil.which("cargo"):
            cmd = ["cargo", "check", "--message-format=json"]
        elif ecosystem == "python":
            py_files = [str(p.relative_to(wt)) for p in wt.rglob("*.py") if ".venv" not in p.parts][:60]
            if py_files:
                cmd = [sys.executable, "-W", "default", "-m", "py_compile", *py_files]

    if not cmd:
        return 0, []

    proc = _run_tool(cmd, cwd=wt, env=env, timeout=300)
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    warnings: list[str] = []

    if "--message-format=json" in cmd:
        for line in (proc.stdout or "").splitlines():
            try:
                msg_obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg_obj.get("reason") == "compiler-message":
                inner = msg_obj.get("message") or {}
                if inner.get("level") == "warning":
                    rendered = str(inner.get("message") or "compiler warning").strip()
                    warnings.append(rendered[:220])
    else:
        for line in combined.splitlines():
            stripped = line.strip()
            if re.search(r"\b(warning|DeprecationWarning|SyntaxWarning)\b", stripped, re.IGNORECASE):
                warnings.append(stripped[:220])

    return len(warnings), warnings[:15]


def detect_flaky_tests(
    wt: Path,
    *,
    ecosystem: str,
    explicit_cmd: list[str] | None,
    runs: int,
    env: dict[str, str],
) -> tuple[bool, list[str]]:
    """Runs the shard test command `runs` times; if pass/fail flips across runs (or individual test
    cases flip), classifies those tests as flaky while distinguishing hard deterministic failures.
    """
    cmd = explicit_cmd
    if cmd is None:
        if ecosystem == "rust" and shutil.which("cargo"):
            cmd = ["cargo", "test", "--no-fail-fast"]
        elif ecosystem == "python" and (wt / "tests").is_dir():
            cmd = [sys.executable, "-m", "pytest", "-q"]
        elif ecosystem == "node" and shutil.which("npm") and (wt / "package.json").is_file():
            cmd = ["npm", "test"]

    if not cmd:
        return True, []

    total_runs = max(1, min(runs, 5))
    outcomes: list[bool] = []
    failed_test_names_by_run: list[set[str]] = []

    for idx in range(total_runs):
        run_env = env.copy()
        run_env["NIGHTWATCH_TEST_PASS_INDEX"] = str(idx + 1)
        proc = _run_tool(cmd, cwd=wt, env=run_env, timeout=300)
        outcomes.append(proc.returncode == 0)
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        failed_names: set[str] = set()
        for ln in combined.splitlines():
            # Pytest `FAILED tests/test_foo.py::test_bar` or Rust `test foo::bar ... FAILED`
            m_py = re.search(r"FAILED\s+([^\s]+::[^\s]+)", ln)
            if m_py:
                failed_names.add(m_py.group(1))
            m_rs = re.search(r"^test\s+([^\s]+)\s+\.\.\.\s+FAILED", ln.strip())
            if m_rs:
                failed_names.add(m_rs.group(1))
        failed_test_names_by_run.append(failed_names)

    flaky: set[str] = set()
    if any(outcomes) and not all(outcomes):
        union_failed = set().union(*failed_test_names_by_run)
        inter_failed = set.intersection(*failed_test_names_by_run) if failed_test_names_by_run else set()
        flaky = union_failed - inter_failed
        if not flaky:
            flaky.add("suite-level-intermittent-failure")

    # Suite is considered passing if the final pass succeeded or at least one pass succeeded without consistent hard failure
    hard_failed = not any(outcomes)
    return (not hard_failed), sorted(flaky)
