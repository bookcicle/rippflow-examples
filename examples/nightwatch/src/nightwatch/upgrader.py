"""Isolated worktree dependency upgrading & breaking-API adaptation:
- `RIPPFLOW_ENV=cloud` (or `ai_upgrader_enabled=False`): 100% deterministic manifest/lockfile upgrader
  (`cargo update`, `npm update --package-lock-only`, or direct `Cargo.toml`/`pyproject.toml`/`package.json`
  semver bumps from audited `outdated_packages`) with ZERO Codex/Gemini CLI or SDK dependency!
- `RIPPFLOW_ENV=local` (`ai_upgrader_enabled=True`): applies deterministic manifest upgrades first, verifies
  whether tests/builds pass, and if breaking API errors or compiler warnings remain, optionally invokes
  Gemini (`google-antigravity`) or Codex (`codex exec --full-auto`) inside the isolated worktree to patch call sites.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .models import is_cloud_env


def _apply_manifest_version_bumps(wt: Path, outdated_packages: list[dict[str, str]]) -> list[str]:
    changed_files: list[str] = []
    if not outdated_packages:
        return changed_files

    # 1. Cargo.toml
    cargo_toml = wt / "Cargo.toml"
    if cargo_toml.is_file():
        orig = cargo_toml.read_text(encoding="utf-8")
        updated = orig
        for pkg in outdated_packages:
            name = re.escape(pkg.get("name", ""))
            latest = pkg.get("latest", "").strip()
            if not name or not latest:
                continue
            updated = re.sub(
                rf'(?m)^(\s*{name}\s*=\s*")([^"]+)(")',
                rf"\g<1>{latest}\g<3>",
                updated,
            )
            updated = re.sub(
                rf'(?m)^(\s*{name}\s*=\s*\{{[^}}]*?\bversion\s*=\s*")([^"]+)(")',
                rf"\g<1>{latest}\g<3>",
                updated,
            )
        if updated != orig:
            cargo_toml.write_text(updated, encoding="utf-8")
            changed_files.append("Cargo.toml")

    # 2. package.json
    pkg_json = wt / "package.json"
    if pkg_json.is_file():
        orig = pkg_json.read_text(encoding="utf-8")
        try:
            data = json.loads(orig)
            touched = False
            for section in ("dependencies", "devDependencies"):
                sec_map = data.get(section)
                if isinstance(sec_map, dict):
                    for pkg in outdated_packages:
                        pname = pkg.get("name", "")
                        latest = pkg.get("latest", "").strip()
                        if pname in sec_map and latest:
                            prefix = "^" if str(sec_map[pname]).startswith("^") else ""
                            new_val = f"{prefix}{latest}"
                            if sec_map[pname] != new_val:
                                sec_map[pname] = new_val
                                touched = True
            if touched:
                pkg_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                changed_files.append("package.json")
        except json.JSONDecodeError:
            pass

    # 3. pyproject.toml / requirements.txt
    req_txt = wt / "requirements.txt"
    if req_txt.is_file():
        orig = req_txt.read_text(encoding="utf-8")
        updated = orig
        for pkg in outdated_packages:
            name = re.escape(pkg.get("name", ""))
            latest = pkg.get("latest", "").strip()
            if name and latest:
                updated = re.sub(
                    rf"(?mi)^({name})\s*(?:==|>=|~=)\s*[0-9A-Za-z._-]+",
                    rf"\g<1>>={latest}",
                    updated,
                )
        if updated != orig:
            req_txt.write_text(updated, encoding="utf-8")
            changed_files.append("requirements.txt")

    return changed_files


def _try_codex_breaking_fix(prompt: str, wt: Path, model: str) -> bool:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return False
    cmd = [codex_bin, "exec", "--full-auto"]
    if model:
        cmd.extend(["-m", model])
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        cwd=str(wt),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return proc.returncode == 0


async def _try_gemini_breaking_fix_async(prompt: str, wt: Path, model: str) -> bool:
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
    from google.antigravity.hooks import policy as agy_policy

    cfg = LocalAgentConfig(
        system_instructions=(
            "Fix breaking dependency API changes or compiler warnings inside this git worktree "
            "with minimal, safe edits so tests pass."
        ),
        capabilities=CapabilitiesConfig(enable_subagents=False),
        policies=[agy_policy.allow_all()],
        workspaces=[str(wt)],
        model=model or None,
    )
    async with Agent(cfg) as agent:
        resp = await agent.chat(prompt)
        async for _ in resp:
            pass
    return True


def upgrade_and_adapt_worktree(
    *,
    wt: Path,
    ecosystem: str,
    outdated_packages: list[dict[str, str]],
    advisories: list[dict[str, str]],
    compiler_warnings_sample: list[str],
    explicit_upgrade_cmd: list[str] | None,
    verify_test_cmd: list[str] | None,
    rippflow_env: str,
    ai_enabled: bool,
    provider: str,
    gemini_model: str,
    codex_model: str,
    env: dict[str, str],
) -> tuple[list[str], str, str]:
    """Returns `(modified_files, upgrader_used, summary)`."""
    cloud_mode = is_cloud_env(rippflow_env)
    upgrader_used = "deterministic:cloud-safe" if cloud_mode else "deterministic"

    # Step 1: Run explicit upgrade command if configured, otherwise apply deterministic manifest/lockfile upgrades
    if explicit_upgrade_cmd:
        subprocess.run(
            explicit_upgrade_cmd,
            cwd=str(wt),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    else:
        _apply_manifest_version_bumps(wt, outdated_packages)
        if ecosystem == "rust" and (wt / "Cargo.lock").is_file() and shutil.which("cargo"):
            subprocess.run(
                ["cargo", "update"],
                cwd=str(wt),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )

    # Step 2: Check whether tests pass after deterministic upgrade; if broken (or warnings exist) and
    # local AI upgrader is enabled (`rippflow_env != "cloud"`), adapt call sites via Gemini/Codex!
    test_failed_output = ""
    if verify_test_cmd:
        tproc = subprocess.run(
            verify_test_cmd,
            cwd=str(wt),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if tproc.returncode != 0:
            test_failed_output = ((tproc.stdout or "") + "\n" + (tproc.stderr or "")).strip()[-2500:]

    needs_ai_fix = bool(test_failed_output or compiler_warnings_sample or advisories)
    if (not cloud_mode) and ai_enabled and provider != "deterministic" and needs_ai_fix:
        prompt = (
            "Overnight repository hygiene & dependency upgrade pass.\n"
            f"Outdated packages bumped: {json.dumps(outdated_packages[:15])}\n"
            f"Security advisories: {json.dumps(advisories[:10])}\n"
            f"Compiler/linter warnings: {json.dumps(compiler_warnings_sample[:10])}\n"
            + (f"Post-upgrade test failure output:\n{test_failed_output}\n" if test_failed_output else "")
            + "\nFix any breaking API call sites or warnings with minimal edits and ensure tests pass."
        )
        if provider in {"auto", "gemini"}:
            try:
                if asyncio.run(_try_gemini_breaking_fix_async(prompt, wt, gemini_model)):
                    upgrader_used = f"local-ai:gemini:{gemini_model}"
            except Exception:
                pass
        if not upgrader_used.startswith("local-ai:") and provider in {"auto", "codex"}:
            try:
                if _try_codex_breaking_fix(prompt, wt, codex_model):
                    upgrader_used = f"local-ai:codex:{codex_model}"
            except Exception:
                pass

    status_proc = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    modified = [ln[3:].strip() for ln in (status_proc.stdout or "").splitlines() if len(ln) > 3]
    summary = (
        f"Updated {len(modified)} file(s) via {upgrader_used}"
        if modified
        else f"Dependencies already clean ({upgrader_used})"
    )
    return modified, upgrader_used, summary
