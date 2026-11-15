#!/usr/bin/env python3
"""
Python Repo Analyst Agent Plugin for Rippflow.
Transport: stdio
Protocol: rippflow-agent/v1

Reads a single JSON request from stdin:
  {"protocol":"rippflow-agent/v1","action":"chat","agent":"py-repo-analyst","prompt":"...","model":"...","cwd":"..."}

Emits rippflow-agent/v1 NDJSON events to stdout:
  {"type":"thinking","text":"..."}
  {"type":"delta","text":"..."}
  {"type":"error","text":"..."}
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Optional, Tuple, List, Dict, Any


def emit(event_type: str, text: str) -> None:
    """Emits a single NDJSON event line to stdout and flushes."""
    line = json.dumps({"type": event_type, "text": text})
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def run_cmd(args: List[str], cwd: str) -> Tuple[int, str, str]:
    """Runs a subprocess command with a safe timeout."""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def scan_workspace(root: str, max_files: int = 5000) -> Tuple[int, List[str]]:
    """Recursively counts files in the workspace, skipping common build/vcs caches."""
    skip_dirs = {
        ".git", "node_modules", "target", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "dist", "build"
    }
    total = 0
    sample_files: List[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".runs")]
            total += len(filenames)
            if len(sample_files) < 10 and filenames:
                rel = os.path.relpath(dirpath, root)
                for f in filenames[:5]:
                    entry = f if rel == "." else os.path.join(rel, f)
                    sample_files.append(entry)
                    if len(sample_files) >= 10:
                        break
            if total > max_files:
                break
    except Exception:
        pass
    return total, sample_files


def query_ollama(model: str, prompt: str, context: Dict[str, Any]) -> Optional[str]:
    """Attempts to query a local Ollama instance if reachable."""
    url = "http://localhost:11434/api/generate"
    system_prompt = (
        "You are an expert repository analyst assistant integrated with Rippflow Studio.\n"
        f"Workspace Directory: {context.get('cwd')}\n"
        f"Git Branch: {context.get('branch', 'unknown')}\n"
        f"Recent Commit: {context.get('commit', 'none')}\n"
        f"Git Status:\n{context.get('git_status', 'clean')}\n"
        f"Scanned Files: ~{context.get('file_count', 0)}\n"
        f"Rippflow Config Active: {context.get('has_rippflow', False)}\n"
        f"Detected Configs: {', '.join(context.get('configs', []))}\n\n"
        f"User Query: {prompt}\n\n"
        "Provide a concise, professional repository summary and answer in Markdown."
    )
    payload = json.dumps({
        "model": model,
        "prompt": system_prompt,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response")
    except Exception:
        return None
    return None


def main() -> None:
    raw_input = sys.stdin.readline()
    if not raw_input or not raw_input.strip():
        raw_input = sys.stdin.read()
    if not raw_input or not raw_input.strip():
        emit("error", "Malformed input: expected rippflow-agent/v1 JSON object on stdin")
        return

    try:
        req = json.loads(raw_input.strip())
    except Exception as err:
        emit("error", f"Malformed JSON: {err}")
        return

    if not isinstance(req, dict):
        emit("error", "Malformed input: expected JSON object")
        return

    protocol = req.get("protocol")
    if protocol != "rippflow-agent/v1":
        emit("error", f"Unsupported protocol '{protocol}', expected 'rippflow-agent/v1'")
        return

    prompt = (req.get("prompt") or "").strip()
    model = req.get("model")
    cwd = req.get("cwd") or os.getcwd()
    cwd = os.path.abspath(cwd)

    emit("thinking", f"Inspecting workspace repository at {cwd}...")

    if not os.path.isdir(cwd):
        emit("error", f"Target directory does not exist: {cwd}")
        return

    # Check git details
    is_git = os.path.exists(os.path.join(cwd, ".git"))
    branch = "n/a"
    recent_commit = "n/a"
    git_status = "Not a git repository"
    git_changes_count = 0

    if is_git:
        emit("thinking", "Inspecting git branch, working tree changes, and recent commit history...")
        code, out, _ = run_cmd(["git", "branch", "--show-current"], cwd)
        if code == 0 and out:
            branch = out
        else:
            code, out, _ = run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd)
            branch = f"detached at {out}" if code == 0 else "unknown"

        code, out, _ = run_cmd(["git", "log", "-1", "--format=%h %s (%cr by %an)"], cwd)
        if code == 0 and out:
            recent_commit = out

        code, out, _ = run_cmd(["git", "status", "--short"], cwd)
        if code == 0:
            if out:
                git_status = out
                git_changes_count = len(out.splitlines())
            else:
                git_status = "Working tree clean (no uncommitted changes)"
                git_changes_count = 0

    emit("thinking", "Scanning directory tree and checking Rippflow configuration...")
    total_files, sample_files = scan_workspace(cwd)

    has_rippflow_yaml = (
        os.path.isfile(os.path.join(cwd, "rippflow.yaml"))
        or os.path.isfile(os.path.join(cwd, "rippflow.yml"))
        or os.path.isdir(os.path.join(cwd, ".rippflow"))
    )

    detected_configs: List[str] = []
    candidate_configs = [
        "rippflow.yaml", "rippflow.yml", "Cargo.toml", "package.json",
        "pyproject.toml", "Makefile", "Dockerfile", ".gitignore"
    ]
    for cfg in candidate_configs:
        if os.path.isfile(os.path.join(cwd, cfg)):
            detected_configs.append(cfg)

    context: Dict[str, Any] = {
        "cwd": cwd,
        "branch": branch,
        "commit": recent_commit,
        "git_status": git_status,
        "file_count": total_files,
        "has_rippflow": has_rippflow_yaml,
        "configs": detected_configs,
    }

    # Query Ollama if model override requested
    if model and str(model).strip():
        model_str = str(model).strip()
        emit("thinking", f"Querying local Ollama endpoint (model: {model_str})...")
        ollama_response = query_ollama(model_str, prompt, context)
        if ollama_response:
            emit("thinking", "Received response from Ollama; delivering repository analysis.")
            emit("delta", ollama_response)
            return
        emit("thinking", "Ollama unreachable or timed out; falling back cleanly to deterministic workspace analysis.")

    emit("thinking", "Synthesizing deterministic repository analysis report...")

    repo_name = os.path.basename(cwd) or cwd
    rippflow_status = "✓ Present" if has_rippflow_yaml else "None detected"

    md: List[str] = [
        f"## Repository Analysis: `{repo_name}`\n",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Workspace Directory** | `{cwd}` |",
        f"| **Git Branch** | `{branch}` |",
        f"| **Latest Commit** | `{recent_commit}` |",
        f"| **Scanned Files** | ~{total_files} |",
        f"| **Rippflow Configuration** | {rippflow_status} |",
        f"| **Detected Manifests/Configs** | {', '.join(f'`{c}`' for c in detected_configs) if detected_configs else 'None'} |",
        "",
        "### Git Working Tree Status",
        "",
    ]

    if is_git:
        if git_changes_count == 0:
            md.append("✓ **Working tree is clean.** No uncommitted modifications or untracked files.\n")
        else:
            md.append(f"Found **{git_changes_count}** uncommitted modification(s):\n")
            md.append("```")
            md.append(git_status)
            md.append("```\n")
    else:
        md.append("⚠️ Workspace is not currently initialized as a Git repository.\n")

    if sample_files:
        md.append("### Workspace Sample Files\n")
        for sf in sample_files:
            md.append(f"- `{sf}`")
        if total_files > len(sample_files):
            md.append(f"- *(and {total_files - len(sample_files)} additional files...)*")
        md.append("")

    if prompt:
        md.append("### Query Response\n")
        md.append(f"**Query**: *\"{prompt}\"*\n")
        md.append(
            f"The workspace `{repo_name}` is currently on branch `{branch}` with "
            f"{git_changes_count} pending git modification(s). "
            f"Rippflow configuration status: {rippflow_status}."
        )
        md.append("")

    emit("delta", "\n".join(md))


if __name__ == "__main__":
    main()
