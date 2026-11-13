"""Environment-aware changelog synthesis:
- `RIPPFLOW_ENV=cloud` (or `ai_changelog_enabled=False`): 100% deterministic Conventional-Commit
  changelog generator with ZERO dependency on `codex` CLI or `google-antigravity` / Gemini SDK!
- `RIPPFLOW_ENV=local` (with `ai_changelog_enabled=True`): uses Codex (`codex exec`) or Gemini
  (`google-antigravity` SDK) when installed locally, gracefully falling back to the deterministic
  changelog builder if neither CLI/SDK is configured on the host.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable

from .models import is_cloud_env


_CONVENTIONAL_RE = re.compile(r"^(?P<sha>[a-f0-9]{6,12})\s+(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?!?:\s*(?P<desc>.+)$")


def build_deterministic_changelog(
    *,
    repo_name: str,
    release_version: str,
    commits: Iterable[str],
    pinned_sha_updates: dict[str, dict[str, str]] | None = None,
) -> str:
    """Pure-Python Conventional Commit changelog builder safe for AWS Lambda / air-gapped cloud cells."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    buckets: dict[str, list[str]] = {
        "Features": [],
        "Fixes": [],
        "Performance & Refactoring": [],
        "Documentation & Maintenance": [],
    }

    commit_list = [c.strip() for c in commits if c.strip()]
    for line in commit_list:
        m = _CONVENTIONAL_RE.match(line)
        if m:
            sha = m.group("sha")[:7]
            ctype = m.group("type").lower()
            scope = m.group("scope")
            desc = m.group("desc").strip()
            formatted = f"- **{scope}**: {desc} (`{sha}`)" if scope else f"- {desc} (`{sha}`)"
            if ctype in {"feat", "feature"}:
                buckets["Features"].append(formatted)
            elif ctype in {"fix", "bugfix", "security", "sec"}:
                buckets["Fixes"].append(formatted)
            elif ctype in {"perf", "refactor"}:
                buckets["Performance & Refactoring"].append(formatted)
            else:
                buckets["Documentation & Maintenance"].append(formatted)
        else:
            parts = line.split(" ", 1)
            if len(parts) == 2 and re.match(r"^[a-f0-9]{6,12}$", parts[0]):
                buckets["Documentation & Maintenance"].append(f"- {parts[1].strip()} (`{parts[0][:7]}`)")
            else:
                buckets["Documentation & Maintenance"].append(f"- {line}")

    lines = [f"## [{release_version}] - {today} ({repo_name})", ""]
    has_any = False
    for section, entries in buckets.items():
        if entries:
            has_any = True
            lines.append(f"### {section}")
            lines.extend(entries)
            lines.append("")

    if pinned_sha_updates:
        has_any = True
        lines.append("### Pinned Dependency SHAs")
        for dep_name, delta in sorted(pinned_sha_updates.items()):
            lines.append(
                f"- `{dep_name}`: `{delta.get('old', 'none')[:7]}` -> `{delta.get('new', '')[:7]}`"
            )
        lines.append("")

    if not has_any:
        lines.append(f"- Synchronized `{repo_name}` release metadata for `v{release_version}`.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _try_codex_changelog(prompt: str, worktree: Path, model: str) -> str | None:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return None
    cmd = [codex_bin, "exec", "-s", "read-only"]
    if model:
        cmd.extend(["-m", model])
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        cwd=str(worktree),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode == 0 and out.startswith("#"):
        return out + "\n"
    return None


async def _try_gemini_changelog_async(prompt: str, worktree: Path, model: str) -> str | None:
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
    from google.antigravity.hooks import policy as agy_policy

    cfg = LocalAgentConfig(
        system_instructions="Draft concise Keep-a-Changelog markdown starting with `## [<version>]` from git commits.",
        capabilities=CapabilitiesConfig(enable_subagents=False),
        policies=[agy_policy.allow_all()],
        workspaces=[str(worktree)],
        model=model or None,
    )
    chunks: list[str] = []
    async with Agent(cfg) as agent:
        resp = await agent.chat(prompt)
        async for tok in resp:
            chunks.append(tok)
    text = "".join(chunks).strip()
    return (text + "\n") if text.startswith("#") else None


def synthesize_changelog(
    *,
    repo_name: str,
    release_version: str,
    commits: list[str],
    pinned_sha_updates: dict[str, dict[str, str]],
    worktree: Path,
    rippflow_env: str,
    ai_enabled: bool,
    provider: str = "auto",
    gemini_model: str = "gemini-4-hydrogen-medium",
    codex_model: str = "gpt-6.1-sol",
) -> tuple[str, str]:
    """Returns `(changelog_markdown, generator_used)`.
    Guarantees zero Codex/Gemini invocation whenever `rippflow_env == 'cloud'` or `ai_enabled` is False!
    """
    if is_cloud_env(rippflow_env) or not ai_enabled or provider == "deterministic":
        return (
            build_deterministic_changelog(
                repo_name=repo_name,
                release_version=release_version,
                commits=commits,
                pinned_sha_updates=pinned_sha_updates,
            ),
            "deterministic:cloud-safe" if is_cloud_env(rippflow_env) else "deterministic",
        )

    prompt = (
        f"Draft a concise markdown release changelog section starting with `## [{release_version}]` "
        f"for repository `{repo_name}` based strictly on these commits and SHA updates:\n\n"
        + "\n".join(commits[:50])
        + (f"\n\nPinned SHA updates: {pinned_sha_updates}" if pinned_sha_updates else "")
    )

    if provider in {"auto", "gemini"}:
        try:
            res = asyncio.run(_try_gemini_changelog_async(prompt, worktree, gemini_model))
            if res:
                return res, f"local-ai:gemini:{gemini_model}"
        except Exception:
            pass

    if provider in {"auto", "codex"}:
        try:
            res = _try_codex_changelog(prompt, worktree, codex_model)
            if res:
                return res, f"local-ai:codex:{codex_model}"
        except Exception:
            pass

    return (
        build_deterministic_changelog(
            repo_name=repo_name,
            release_version=release_version,
            commits=commits,
            pinned_sha_updates=pinned_sha_updates,
        ),
        "deterministic:fallback",
    )
