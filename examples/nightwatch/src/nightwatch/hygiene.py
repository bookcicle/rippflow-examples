"""Workspace hygiene & disk reclamation:
1. Prune broken git worktree metadata + remove clean, stale non-primary linked git worktrees older than `stale_worktree_hours`.
2. Reclaim Rust Cargo `target/` directories (plus any extra `.cargo-target-*` dirs) with exact byte accounting.
3. Clean dangling/unused Docker images & build cache (`docker image prune` + `docker builder prune`) when Docker daemon is reachable.

Supports `execution_mode == "dry_run"` (calculates exact reclaimable bytes without deleting host directories)
and `execution_mode == "live"` (executes real cleanup safely, never touching dirty worktrees or primary checkouts).
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from .models import HygieneOptions, NightwatchRepoSpec, is_cloud_env


_DOCKER_RECLAIMED_RE = re.compile(r"Total reclaimed space:\s*([0-9.]+)\s*([kKmMgGtTpP]?i?[bB])")


def format_bytes(num_bytes: int) -> str:
    val = float(max(0, num_bytes))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024.0 or unit == "TiB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TiB"


def _parse_human_size(num_str: str, unit_str: str) -> int:
    val = float(num_str)
    u = unit_str.upper()
    multipliers = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
    }
    return int(val * multipliers.get(u, 1))


def directory_size_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                if not fpath.is_symlink():
                    total += fpath.stat().st_size
            except OSError:
                pass
    return total


def parse_git_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line[len("worktree ") :].strip(), "prunable": False, "bare": False, "locked": False}
        elif line.startswith("HEAD "):
            current["head"] = line[5:].strip()
        elif line.startswith("branch "):
            current["branch"] = line[7:].strip()
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("prunable"):
            current["prunable"] = True
        elif line.startswith("locked"):
            current["locked"] = True
    if current:
        entries.append(current)
    return entries


def clean_stale_repo_worktrees(
    repos: dict[str, NightwatchRepoSpec],
    *,
    stale_hours: float,
    execution_mode: str,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now = now_ts if now_ts is not None else time.time()
    cutoff_sec = max(0.0, stale_hours * 3600.0)
    removed_worktrees: list[dict[str, Any]] = []
    skipped_dirty: list[str] = []
    pruned_metadata_repos: list[str] = []
    bytes_reclaimed = 0

    for name, spec in repos.items():
        repo_root = Path(spec.path).resolve()
        if not (repo_root / ".git").exists():
            continue

        list_proc = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if list_proc.returncode != 0:
            continue

        worktrees = parse_git_worktree_porcelain(list_proc.stdout)
        # Index 0 is ALWAYS the primary repository checkout — never remove it!
        for wt_info in worktrees[1:]:
            wt_path = Path(wt_info["path"])
            if wt_info.get("locked"):
                continue

            if wt_info.get("prunable") or not wt_path.exists():
                if execution_mode == "live":
                    subprocess.run(
                        ["git", "-C", str(repo_root), "worktree", "prune"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                    )
                if name not in pruned_metadata_repos:
                    pruned_metadata_repos.append(name)
                continue

            try:
                mtime = wt_path.stat().st_mtime
            except OSError:
                continue

            age_sec = max(0.0, now - mtime)
            if age_sec < cutoff_sec:
                continue

            # Never remove a linked worktree with uncommitted user modifications!
            dirty_probe = subprocess.run(
                ["git", "-C", str(wt_path), "status", "--porcelain"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if dirty_probe.returncode != 0 or dirty_probe.stdout.strip():
                skipped_dirty.append(str(wt_path))
                continue

            wt_bytes = directory_size_bytes(wt_path)
            if execution_mode == "live":
                rm_proc = subprocess.run(
                    ["git", "-C", str(repo_root), "worktree", "remove", str(wt_path)],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if rm_proc.returncode != 0:
                    continue
                subprocess.run(
                    ["git", "-C", str(repo_root), "worktree", "prune"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                )

            bytes_reclaimed += wt_bytes
            removed_worktrees.append(
                {
                    "repo": name,
                    "path": str(wt_path),
                    "ageHours": round(age_sec / 3600.0, 1),
                    "bytesReclaimed": wt_bytes,
                    "dryRun": execution_mode != "live",
                }
            )

    return {
        "removedWorktrees": removed_worktrees,
        "prunedMetadataRepos": pruned_metadata_repos,
        "skippedDirtyWorktrees": skipped_dirty,
        "bytesReclaimed": bytes_reclaimed,
    }


def reclaim_cargo_targets(
    repos: dict[str, NightwatchRepoSpec],
    *,
    extra_target_dirs: list[str],
    execution_mode: str,
) -> dict[str, Any]:
    cleaned_targets: list[dict[str, Any]] = []
    bytes_reclaimed = 0
    seen: set[Path] = set()

    candidates: list[tuple[str, Path]] = []
    for name, spec in repos.items():
        rpath = Path(spec.path).resolve()
        if (rpath / "Cargo.toml").is_file() and (rpath / "target").is_dir():
            candidates.append((name, rpath / "target"))

    for extra in extra_target_dirs:
        epath = Path(extra).resolve()
        if epath.is_dir() and (epath.name == "target" or epath.name.startswith(".cargo-target")):
            candidates.append(("extra-cargo-target", epath))

    for owner, target_dir in candidates:
        if target_dir in seen or not target_dir.is_dir():
            continue
        seen.add(target_dir)

        # Safety guard: only ever delete directories named `target` inside a Cargo workspace or `.cargo-target*`
        if target_dir.name != "target" and not target_dir.name.startswith(".cargo-target"):
            continue

        sz = directory_size_bytes(target_dir)
        if execution_mode == "live":
            # Direct `shutil.rmtree` avoids downloading missing crates.io index metadata when `cargo clean`
            # runs on an unindexed crate or air-gapped runner, keeping Phase 1 strictly O(fs unlink)!
            shutil.rmtree(target_dir, ignore_errors=True)

        bytes_reclaimed += sz
        cleaned_targets.append(
            {
                "repo": owner,
                "targetDir": str(target_dir),
                "bytesReclaimed": sz,
                "humanSize": format_bytes(sz),
                "dryRun": execution_mode != "live",
            }
        )

    return {
        "cleanedTargets": cleaned_targets,
        "bytesReclaimed": bytes_reclaimed,
    }


def reclaim_docker_images(
    *,
    rippflow_env: str,
    prune_until: str,
    execution_mode: str,
    which_fn=shutil.which,
    runner=subprocess.run,
) -> dict[str, Any]:
    if is_cloud_env(rippflow_env):
        return {
            "status": "skipped_in_cloud",
            "bytesReclaimed": 0,
            "detail": "Docker host prune skipped when RIPPFLOW_ENV=cloud",
        }

    docker_bin = which_fn("docker")
    if not docker_bin:
        return {
            "status": "docker_not_installed",
            "bytesReclaimed": 0,
            "detail": "docker CLI not found on PATH",
        }

    info_probe = runner(
        [docker_bin, "system", "df", "--format", "{{json .}}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if info_probe.returncode != 0:
        return {
            "status": "daemon_unreachable",
            "bytesReclaimed": 0,
            "detail": "Docker daemon is not running or unreachable",
        }

    if execution_mode != "live":
        return {
            "status": "dry_run_ready",
            "bytesReclaimed": 0,
            "systemDfSample": [ln for ln in (info_probe.stdout or "").splitlines()[:6] if ln.strip()],
            "plannedCommand": f"docker image prune -af --filter until={prune_until} && docker builder prune -f --filter until={prune_until}",
        }

    reclaimed_bytes = 0
    outputs: list[str] = []
    for subcmd in (
        [docker_bin, "image", "prune", "-af", "--filter", f"until={prune_until}"],
        [docker_bin, "builder", "prune", "-f", "--filter", f"until={prune_until}"],
    ):
        proc = runner(
            subcmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        out = (proc.stdout or "").strip()
        if out:
            outputs.append(out[-500:])
            m = _DOCKER_RECLAIMED_RE.search(out)
            if m:
                reclaimed_bytes += _parse_human_size(m.group(1), m.group(2))

    return {
        "status": "pruned",
        "bytesReclaimed": reclaimed_bytes,
        "humanSize": format_bytes(reclaimed_bytes),
        "output": outputs,
    }


def execute_hygiene_sweep(
    *,
    repos: dict[str, NightwatchRepoSpec],
    options: HygieneOptions,
    rippflow_env: str,
    execution_mode: str,
) -> dict[str, Any]:
    wt_res = (
        clean_stale_repo_worktrees(
            repos,
            stale_hours=options.stale_worktree_hours,
            execution_mode=execution_mode,
        )
        if options.clean_stale_worktrees
        else {"removedWorktrees": [], "prunedMetadataRepos": [], "skippedDirtyWorktrees": [], "bytesReclaimed": 0}
    )

    cargo_res = (
        reclaim_cargo_targets(
            repos,
            extra_target_dirs=options.extra_cargo_target_dirs,
            execution_mode=execution_mode,
        )
        if options.delete_cargo_targets
        else {"cleanedTargets": [], "bytesReclaimed": 0}
    )

    docker_res = (
        reclaim_docker_images(
            rippflow_env=rippflow_env,
            prune_until=options.docker_prune_until,
            execution_mode=execution_mode,
        )
        if options.clean_docker_images
        else {"status": "disabled", "bytesReclaimed": 0}
    )

    total_bytes = (
        int(wt_res.get("bytesReclaimed", 0))
        + int(cargo_res.get("bytesReclaimed", 0))
        + int(docker_res.get("bytesReclaimed", 0))
    )
    return {
        "executionMode": execution_mode,
        "rippflowEnv": rippflow_env,
        "totalBytesReclaimed": total_bytes,
        "totalHumanReclaimed": format_bytes(total_bytes),
        "worktrees": wt_res,
        "cargoTargets": cargo_res,
        "docker": docker_res,
    }
