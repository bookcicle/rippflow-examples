"""Author and Reviewer agent drivers (`agy` CLI / Antigravity SDK and `codex` CLI).

Enforces strict per-phase GitHub identity separation:
- Author phase runs ONLY with `authorGhUser` (`GH_TOKEN` + git author/committer env).
- Reviewer phase runs ONLY with `reviewerGhUser` (`GH_TOKEN`), read-only repo access,
  and posts comments (`gh pr review --comment`) or approvals (`gh pr review --approve`).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any
import uuid

from .models import (
    PipelineConfig,
    ShardState,
    build_scoped_git_gh_env,
    format_bounded_test_output,
    resolve_gh_token,
)
from .visual_qa import pin_mock_screenshots_to_pr


REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "comments"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "comment"],
            "description": "Use 'approve' ONLY when zero actionable defects or comments remain; otherwise 'comment'.",
        },
        "summary": {
            "type": "string",
            "description": "Concise overall review summary.",
        },
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "line", "severity", "body"],
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["P1", "P2", "P3"]},
                    "body": {"type": "string"},
                },
            },
        },
    },
}


def _run_git(worktree: Path, args: list[str], env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(worktree), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr or proc.stdout}")
    return proc


def _configure_worktree_author_git(worktree: Path, cfg: PipelineConfig, author_env: dict[str, str]) -> None:
    email = cfg.author_git_email or f"{cfg.author_gh_user}@users.noreply.github.com"
    _run_git(worktree, ["config", "user.name", cfg.author_gh_user], env=author_env)
    _run_git(worktree, ["config", "user.email", email], env=author_env)
    if cfg.author_signing_key:
        _run_git(worktree, ["config", "commit.gpgsign", "true"], env=author_env)
        _run_git(worktree, ["config", "user.signingkey", cfg.author_signing_key], env=author_env)
    else:
        # Prevent inheriting a host user's SSH signing key for a different GitHub bot account!
        _run_git(worktree, ["config", "commit.gpgsign", "false"], env=author_env)


def _hydrate_pr_review_context_if_needed(
    *,
    cfg: PipelineConfig,
    state: ShardState,
    wt: Path,
    gh_env: dict[str, str],
) -> None:
    """When resuming an existing PR branch across runs, hydrate PR number/url, prior reviewer
    comments, and the latest reviewed commit SHA from GitHub so neither author nor Codex starts cold.
    """
    if cfg.execution_mode != "live":
        return

    repo_args = ["--repo", state.github_repo] if state.github_repo else []
    if state.pr_number is None and state.pr_url is None:
        existing = subprocess.run(
            ["gh", "pr", "list", *repo_args, "--head", state.branch_name, "--json", "number,url"],
            cwd=str(wt),
            env=gh_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            items = json.loads(existing.stdout)
            if items:
                state.pr_number = int(items[0]["number"])
                state.pr_url = str(items[0]["url"])

    if (state.pr_number is None and state.pr_url is None) or state.review_ledger:
        return

    pr_ref = state.pr_url or str(state.pr_number)
    view_args = ["gh", "pr", "view", pr_ref]
    if state.github_repo and not state.pr_url:
        view_args.extend(["--repo", state.github_repo])
    view_args.extend(["--json", "number,url,headRefOid,reviews"])
    view = subprocess.run(
        view_args,
        cwd=str(wt),
        env=gh_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if view.returncode != 0 or not view.stdout.strip():
        return

    info = json.loads(view.stdout)
    state.pr_number = int(info.get("number") or state.pr_number or 0) or state.pr_number
    state.pr_url = str(info.get("url") or state.pr_url or "") or state.pr_url
    reviews = [
        r
        for r in (info.get("reviews") or [])
        if (r.get("author") or {}).get("login") == cfg.reviewer_gh_user and (r.get("body") or "").strip()
    ]
    if not reviews:
        return

    bullet_re = re.compile(r"-\s*\*\*\[([A-Za-z0-9_-]+)\]\*\*\s*`([^:`]+):(\d+)`\s*(?:—|--|-)\s*(.+)")
    for idx, rev in enumerate(reviews, start=1):
        body_txt = str(rev.get("body") or "").strip()
        commit_oid = str((rev.get("commit") or {}).get("oid") or "").strip() or None
        parsed_comments: list[dict[str, Any]] = []
        for line in body_txt.splitlines():
            m = bullet_re.match(line.strip())
            if m:
                parsed_comments.append(
                    {
                        "severity": m.group(1),
                        "path": m.group(2),
                        "line": int(m.group(3)),
                        "body": m.group(4).strip(),
                    }
                )
        state.review_ledger.append(
            {
                "turn": idx,
                "commitOid": commit_oid,
                "verdict": "approve" if rev.get("state") == "APPROVED" else "comment",
                "summary": body_txt.splitlines()[2] if len(body_txt.splitlines()) >= 3 else body_txt[:300],
                "comments": parsed_comments,
            }
        )
        if commit_oid:
            state.last_reviewed_commit = commit_oid

    latest = state.review_ledger[-1]
    head_oid_proc = _run_git(wt, ["rev-parse", "HEAD"], env=gh_env, check=False)
    local_head_oid = head_oid_proc.stdout.strip() if head_oid_proc.returncode == 0 else ""
    if (
        latest["verdict"] == "comment"
        and latest["comments"]
        and latest.get("commitOid")
        and latest["commitOid"] == local_head_oid
        and not state.pending_review_comments
    ):
        state.last_review_verdict = "comment"
        state.last_review_summary = str(latest.get("summary") or "")
        state.pending_review_comments = list(latest["comments"])


def _format_prior_review_ledger(review_ledger: list[dict[str, Any]], max_turns_shown: int = 5) -> str:
    """Formats prior review turns concisely so both Author and Codex see prior requested edits."""
    if not review_ledger:
        return ""
    lines: list[str] = []
    for entry in review_ledger[-max_turns_shown:]:
        t = entry.get("turn", "?")
        oid = str(entry.get("commitOid") or "")[:7] or "unknown"
        verdict = entry.get("verdict", "comment")
        summary = str(entry.get("summary") or "").strip()
        lines.append(f"### Turn {t} Review (commit `{oid}`, verdict `{verdict}`)")
        if summary:
            lines.append(f"Summary: {summary}")
        for c in entry.get("comments") or []:
            lines.append(
                f"- [{c.get('severity', 'P2')}] `{c.get('path', '?')}:{c.get('line', 1)}` — {c.get('body', '')}"
            )
    return "\n".join(lines)


def _extract_codex_thread_id(jsonl_stdout: str) -> str | None:
    for raw_line in jsonl_stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            return str(ev["thread_id"])
    return None


def _effective_shard_prompt(cfg: PipelineConfig, shard: str) -> str:
    spec = cfg.repos.get(shard)
    if spec and spec.prompt_file and Path(spec.prompt_file).is_file():
        return Path(spec.prompt_file).read_text(encoding="utf-8").strip()
    return cfg.feature_request


def _collect_visual_attachment_args(state: ShardState, *, include_actual: bool = True) -> tuple[list[str], str]:
    """Builds `-i <image>` CLI flags and an explicit image manifest section for multimodal turns."""
    image_flags: list[str] = []
    manifest_lines: list[str] = []
    idx = 0

    for raw_p in state.mock_screenshot_paths:
        p = Path(raw_p)
        if p.is_file():
            idx += 1
            resolved = p.resolve()
            image_flags.extend(["-i", str(p)])
            manifest_lines.append(
                f"- Image #{idx} (`TARGET MOCK`): `{p.name}` (`{resolved}`) — authoritative target visual design."
            )

    if include_actual:
        for raw_p in state.actual_screenshot_paths:
            p = Path(raw_p)
            if p.is_file():
                idx += 1
                resolved = p.resolve()
                image_flags.extend(["-i", str(p)])
                manifest_lines.append(
                    f"- Image #{idx} (`ACTUAL RENDERED UI`): `{p.name}` (`{resolved}`) — actual screenshot captured from the branch."
                )

    return image_flags, "\n".join(manifest_lines)


def _build_author_prompt(cfg: PipelineConfig, state: ShardState, turn: int) -> str:
    spec_text = _effective_shard_prompt(cfg, state.shard)
    _, img_manifest = _collect_visual_attachment_args(state, include_actual=(turn > 1))
    visual_note = ""
    if img_manifest:
        visual_note = (
            "\n\n## Attached Visual Reference Screenshots\n"
            f"{img_manifest}\n"
            "- If your harness uses file tools (e.g. `agy` CLI), call `read_file` (or `view_file`) on each image path above to inspect the visual mock and captured screenshots directly.\n"
            "- Match the attached `TARGET MOCK` visual hierarchy, colors, column layout, and component styling faithfully.\n"
            "- If `RIPPFLOW_UI_SCREENSHOT_DIR` screenshot capture is requested in the specification (or `screenshotCommand` is configured), "
            "ensure the offscreen screenshot test writes crisp PNGs of the rendered UI into `std::env::var(\"RIPPFLOW_UI_SCREENSHOT_DIR\")` "
            "so `local_test_suite` and the reviewer can visually compare your rendered UI against the mock!"
        )
    regression_note = ""
    if cfg.regression_mode:
        regression_note = (
            "\n\n## Mandatory Regression Workflow (History -> Reproducing Test -> Patch)\n"
            "1. **Inspect Git History First**: Before editing any code, inspect recent commits (`git log --oneline -n 25`, "
            "`git show <sha>`, `git log -S` / `-G`, `git blame`) to pinpoint the exact commit SHA(s) and code changes that "
            "broke this behavior. Cite the root-cause commit SHA(s) and exact mechanism in your final summary.\n"
            "2. **Write a Reproducing Test First**: Add deterministic automated regression test(s) covering the broken "
            "interaction/state path BEFORE patching production logic so the test would fail against the regressed code.\n"
            "3. **Patch Surgically Without Secondary Regressions**: Fix the root cause cleanly while preserving the feature "
            "or behavior that the introducing commit added."
        )
    if turn == 1 and not state.pending_review_comments and state.last_test_passed is not False:
        return (
            f"You are working in repository `{state.shard}` at `{state.worktree_path}` on branch `{state.branch_name}` "
            f"(based on `{state.base_branch}`).\n\n"
            f"## Full Prompt Specification\n{spec_text}"
            f"{regression_note}"
            f"{visual_note}\n\n"
            "## Instructions\n"
            "1. First inspect `git log -n 3 --oneline` and `git diff origin/main...HEAD --stat` — if this branch already has "
            "an in-progress implementation, refine/complete what is missing rather than starting from scratch.\n"
            "2. Work efficiently: read only the specific line ranges needed (batching reads where possible).\n"
            "3. Implement the requested specification and its unit tests cleanly.\n"
            "4. Do NOT launch background/async `cargo` commands (`WaitMsBeforeAsync` must be large enough or skip running `cargo` "
            "entirely) — the dedicated `local_test_suite` phase immediately runs the full test suite next and feeds back any "
            "compiler/test errors automatically!\n"
            "5. Stage and commit your changes on the current branch (`git add -A && git commit -m ...`).\n"
            "6. Do NOT switch branches and do NOT merge or approve PRs.\n"
            "Summarize the files changed concisely."
        )

    feedback_sections: list[str] = [
        f"Turn {turn} of {cfg.max_turns} rework on repository `{state.shard}` (branch `{state.branch_name}`).",
        (
            f"## Rework Fast-Path Instructions (Turn {turn} of {cfg.max_turns})\n"
            "1. Prior turn changes are already committed on this branch (`git log -n 3 --oneline`, "
            "`git diff origin/main...HEAD --stat`). Do **NOT** re-scan large files or re-implement from scratch.\n"
            "2. Read **only ±30 lines** around the exact `file:line` locations in `Local Test Suite / Screenshot Failure` "
            "or `Current Open Code Review Comments` below.\n"
            "3. Apply surgical fixes without breaking previously resolved items, commit (`git add -A && git commit`), "
            "and finish promptly well under timeout."
        ),
    ]
    if state.last_test_passed is False and state.last_test_output:
        bounded_test_out = format_bounded_test_output(state.last_test_output, max_chars=12000)
        feedback_sections.append(
            "## Local Test Suite / Screenshot Failure (Must Fix)\n"
            f"```\n{bounded_test_out}\n```"
        )
    if state.pending_review_comments:
        formatted = "\n".join(
            f"- [{c.get('severity', 'P2')}] `{c.get('path', '?')}:{c.get('line', 1)}` — {c.get('body', '')}"
            for c in state.pending_review_comments
        )
        feedback_sections.append(
            f"## Current Open Code Review Comments from `{cfg.reviewer_gh_user}` (Must Address All)\n"
            f"Summary: {state.last_review_summary}\n{formatted}"
        )
    if img_manifest:
        feedback_sections.append(
            "## Attached Visual QA Comparison Images (Mock vs. Actual Rendered UI)\n"
            f"{img_manifest}\n"
            "- Call `read_file` (or `view_file`) on each attached image path above to compare the target mock against the rendered UI."
        )
    if len(state.review_ledger) > 1:
        earlier_ledger = _format_prior_review_ledger(state.review_ledger[:-1], max_turns_shown=4)
        if earlier_ledger:
            feedback_sections.append(
                "## Earlier Review History (Do Not Regress Previously Resolved Items)\n" + earlier_ledger
            )
    feedback_sections.append(f"## Reference: Original Prompt Specification\n{spec_text}")
    feedback_sections.append(
        "Apply targeted fixes in this worktree without breaking previously resolved review items "
        "(the `local_test_suite` phase will re-run tests and re-capture UI screenshots automatically right after this turn), "
        "and commit your updates to the current branch."
    )
    return "\n\n".join(feedback_sections)


def _invoke_codex_author_agent(
    *,
    prompt: str,
    worktree: Path,
    model_name: str,
    agent_env: dict[str, str],
    session_id: str | None = None,
    image_flags: list[str] | None = None,
    repo_path: Path | None = None,
) -> tuple[str, str | None]:
    """Executes `codex exec` (or `codex exec resume`) directly inside the isolated worktree."""
    img_args = list(image_flags or [])
    extra_dirs = ["--add-dir", str(worktree.parent)]
    if repo_path and (repo_path / ".git").exists():
        extra_dirs.extend(["--add-dir", str(repo_path / ".git")])

    with tempfile.TemporaryDirectory(prefix="rippflow-codex-author-") as tmp:
        out_path = Path(tmp) / "author_summary.txt"
        proc: subprocess.CompletedProcess[str] | None = None
        if session_id:
            resume_cmd = [
                "codex",
                "exec",
                "resume",
                "--skip-git-repo-check",
                "-c",
                'approval_policy="never"',
                "-c",
                'sandbox_mode="workspace-write"',
                "-m",
                model_name,
                *img_args,
                "-o",
                str(out_path),
                "--json",
                session_id,
                "-",
            ]
            proc = subprocess.run(
                resume_cmd,
                cwd=str(worktree),
                env=agent_env,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=840,
                check=False,
            )
            if proc.returncode != 0:
                proc = None
                session_id = None

        if proc is None:
            fresh_cmd = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "-C",
                str(worktree),
                *extra_dirs,
                "-s",
                "workspace-write",
                "-c",
                'approval_policy="never"',
                "-m",
                model_name,
                *img_args,
                "-o",
                str(out_path),
                "--json",
                "-",
            ]
            proc = subprocess.run(
                fresh_cmd,
                cwd=str(worktree),
                env=agent_env,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=840,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"codex exec author failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-2000:]}"
                )

        detected = _extract_codex_thread_id(proc.stdout)
        if detected:
            session_id = detected
        summary = out_path.read_text(encoding="utf-8").strip() if out_path.is_file() else (proc.stdout or "")[-1000:]
        return summary, session_id


async def _invoke_antigravity_agent(
    *,
    prompt: str,
    worktree: Path,
    session_dir: Path,
    conversation_id: str,
    model_name: str,
    agent_env: dict[str, str],
) -> str:
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
    from google.antigravity.hooks import policy as agy_policy
    from google.antigravity.types import (
        BudgetConfig,
        BudgetScope,
        CompactionConfig,
        ModelAPIRetryConfig,
        RetryConfig,
        SessionContinuationMode,
    )
    import os
    import re

    session_dir.mkdir(parents=True, exist_ok=True)
    app_data_dir = session_dir / "app_data"
    app_data_dir.mkdir(parents=True, exist_ok=True)
    system_instructions = (
        "You are an autonomous software engineer authoring clean, tested pull requests. "
        "Work strictly inside the provided workspace directory, use search_directory + targeted view_file ranges "
        "(avoid sequential full-file scans), edit files cleanly, avoid running slow builds when a dedicated test "
        "phase runs next, and commit your changes to the checked-out git branch."
    )
    resolved_api_key = (
        agent_env.get("GEMINI_API_KEY")
        or agent_env.get("GOOGLE_GEMINI_API_KEY")
        or agent_env.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )

    async def _run_with_model(selected_model: str | None) -> str:
        config_kwargs: dict[str, Any] = {
            "system_instructions": system_instructions,
            "capabilities": CapabilitiesConfig(
                enable_subagents=False,
                compaction_threshold=120_000,
            ),
            "policies": [agy_policy.allow_all()],
            "workspaces": [str(worktree)],
            "env": agent_env,
            "conversation_id": conversation_id,
            "session_continuation_mode": SessionContinuationMode.CREATE_OR_RESUME,
            "save_dir": str(session_dir),
            "app_data_dir": str(app_data_dir),
            "compaction_config": CompactionConfig(token_threshold=120_000),
            "budget_config": BudgetConfig(
                max_tool_calls=40,
                scope=BudgetScope.FORWARD_LOOKING,
            ),
            "retry_config": RetryConfig(
                api_retry=ModelAPIRetryConfig(
                    max_retries=5,
                    initial_sleep_duration_ms=4000,
                    exponential_multiplier=2.0,
                )
            ),
        }
        if resolved_api_key:
            config_kwargs["api_key"] = resolved_api_key
        if selected_model:
            config_kwargs["model"] = selected_model
        config = LocalAgentConfig(**config_kwargs)

        for rate_attempt in range(4):
            try:
                chunks: list[str] = []
                async with Agent(config) as agent:
                    response = await agent.chat(prompt)
                    async for token in response:
                        chunks.append(token)
                return "".join(chunks).strip()
            except Exception as err:
                err_txt = str(err)
                is_rate_limit = "429" in err_txt or "RESOURCE_EXHAUSTED" in err_txt or "503" in err_txt
                if is_rate_limit and rate_attempt < 3:
                    m = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", err_txt, re.IGNORECASE)
                    wait_secs = min(90.0, (float(m.group(1)) + 3.0) if m else (20.0 * (rate_attempt + 1)))
                    await asyncio.sleep(wait_secs)
                    continue
                raise
        return ""

    try:
        return await _run_with_model(model_name)
    except Exception as primary_err:
        if model_name and ("model" in str(primary_err).lower() or "not found" in str(primary_err).lower()):
            # Graceful fallback to SDK default Gemini Flash model if preview alias differs
            return await _run_with_model(None)
        raise


def _resolve_agy_bin() -> str:
    cand = shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
    if Path(cand).is_file() and os.access(cand, os.X_OK):
        return cand
    raise RuntimeError(f"`agy` CLI binary not found or not executable (checked PATH and {Path.home() / '.local' / 'bin' / 'agy'})")


def _extract_agy_json_output(stdout: str) -> dict[str, Any]:
    for raw_line in reversed(stdout.splitlines()):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and (
            "status" in parsed or "response" in parsed or "conversation_id" in parsed or "structured_output" in parsed
        ):
            return parsed
    raise RuntimeError(f"agy CLI did not emit valid JSON output: {stdout[-2000:]}")


def _parse_review_json_fallback(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return json.loads(stripped)


def _invoke_agy_cli_agent(
    *,
    prompt: str,
    worktree: Path,
    model_name: str,
    agent_env: dict[str, str],
    read_only: bool,
    session_id: str | None = None,
    json_schema: dict[str, Any] | None = None,
    image_paths: list[str] | None = None,
    repo_path: Path | None = None,
    extra_add_dirs: list[str] | None = None,
    timeout_sec: int = 840,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Executes non-interactive `agy` CLI (`--print=... --output-format json`) using the user's signed-in session."""
    agy_bin = _resolve_agy_bin()
    with tempfile.TemporaryDirectory(prefix="rippflow-agy-") as tmp:
        tmp_dir = Path(tmp)
        add_dirs: list[str] = []
        for cand_dir in [
            str(tmp_dir),
            str(worktree.parent),
            *(
                [str(repo_path / ".git")]
                if (not read_only and repo_path and (repo_path / ".git").exists())
                else []
            ),
            *(extra_add_dirs or []),
        ]:
            if cand_dir and cand_dir not in add_dirs:
                add_dirs.append(cand_dir)

        for raw_img in image_paths or []:
            img_p = Path(raw_img)
            if img_p.is_file():
                parent_dir = str(img_p.resolve().parent)
                if parent_dir not in add_dirs:
                    add_dirs.append(parent_dir)

        schema_path: Path | None = None
        if json_schema is not None:
            schema_path = tmp_dir / "output_schema.json"
            schema_path.write_text(json.dumps(json_schema, indent=2), encoding="utf-8")

        if len(prompt.encode("utf-8")) > 60_000:
            prompt_file = tmp_dir / "prompt_instructions.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            effective_prompt = (
                f"Read the complete instructions from `{prompt_file}` using `read_file` (or `view_file`) "
                "and execute them completely."
            )
        else:
            effective_prompt = prompt

        def _build_cmd(resume_conv_id: str | None) -> list[str]:
            cmd = [
                agy_bin,
                "--model",
                model_name,
                "--dangerously-skip-permissions",
                "--disable-slash-commands",
                "--output-format",
                "json",
            ]
            if read_only:
                cmd.append("--sandbox")
            for extra_dir in add_dirs:
                cmd.extend(["--add-dir", extra_dir])
            if schema_path is not None:
                cmd.extend(["--json-schema", str(schema_path)])
            if resume_conv_id:
                cmd.extend(["--conversation", resume_conv_id])
            cmd.append(f"--print={effective_prompt}")
            return cmd

        initial_head = ""
        if not read_only:
            initial_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        active_session = session_id
        last_error: str | None = None
        for attempt in range(4):
            try:
                proc = subprocess.run(
                    _build_cmd(active_session),
                    cwd=str(worktree),
                    env=agent_env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                if not read_only:
                    dirty = subprocess.run(
                        ["git", "-C", str(worktree), "status", "--porcelain"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip()
                    curr_head = subprocess.run(
                        ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip()
                    if dirty or (initial_head and curr_head and curr_head != initial_head):
                        return (
                            f"agy CLI hit {timeout_sec}s limit after producing worktree changes; proceeding to local_test_suite.",
                            None,
                            active_session,
                        )
                raise
            combined = f"{proc.stderr}\n{proc.stdout}".strip()
            parsed: dict[str, Any] | None = None
            if proc.stdout.strip():
                try:
                    parsed = _extract_agy_json_output(proc.stdout)
                except RuntimeError:
                    parsed = None

            if (
                proc.returncode == 0
                and parsed is not None
                and str(parsed.get("status") or "").upper() != "ERROR"
            ):
                new_conv_id = str(parsed.get("conversation_id") or active_session or "").strip() or None
                response_text = str(parsed.get("response") or "").strip()
                structured = parsed.get("structured_output")
                structured_dict = structured if isinstance(structured, dict) else None
                return response_text, structured_dict, new_conv_id

            err_detail = str(parsed.get("error") or "") if parsed else ""
            err_txt = f"{err_detail}\n{combined}".strip()
            is_rate_limit = any(
                marker in err_txt.upper()
                for marker in ("429", "RESOURCE_EXHAUSTED", "503", "RATE LIMIT", "UNAVAILABLE")
            )
            if is_rate_limit and attempt < 3:
                m = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", err_txt, re.IGNORECASE)
                wait_secs = min(90.0, (float(m.group(1)) + 3.0) if m else (20.0 * (attempt + 1)))
                time.sleep(wait_secs)
                continue

            if active_session is not None:
                # Fall back to a fresh conversation once if `--conversation` cannot be resumed.
                active_session = None
                last_error = err_txt[-2000:]
                continue

            last_error = err_txt[-2000:]
            break

        raise RuntimeError(f"agy CLI failed (model={model_name}): {last_error or 'unknown error'}")


def _ensure_committed_and_pr_opened(
    *,
    cfg: PipelineConfig,
    state: ShardState,
    turn: int,
    author_env: dict[str, str],
    summary_note: str,
) -> None:
    wt = Path(state.worktree_path)
    status = _run_git(wt, ["status", "--porcelain"], env=author_env).stdout.strip()
    if status:
        _run_git(wt, ["add", "-A"], env=author_env)
        commit_msg = (
            f"feat({state.shard}): implement requested feature"
            if turn == 1
            else f"fix({state.shard}): address turn {turn} feedback"
        )
        _run_git(wt, ["commit", "-m", commit_msg], env=author_env)

    if cfg.execution_mode != "live":
        return

    ahead_proc = _run_git(
        wt,
        ["rev-list", "--count", f"origin/{state.base_branch}..HEAD"],
        env=author_env,
        check=False,
    )
    ahead_count = int((ahead_proc.stdout or "0").strip() or "0") if ahead_proc.returncode == 0 else 1
    if ahead_count == 0 and state.pr_number is None:
        # Leave PR creation until at least one commit is present so `local_test_suite` can trigger rework cleanly
        return

    push_target = f"https://github.com/{state.github_repo}.git" if state.github_repo else "origin"
    gh_bin = shutil.which("gh") or "gh"
    _run_git(
        wt,
        [
            "-c",
            "credential.helper=",
            "-c",
            f"credential.helper=!{gh_bin} auth git-credential",
            "push",
            push_target,
            f"HEAD:refs/heads/{state.branch_name}",
        ],
        env=author_env,
    )

    if state.pr_number is None:
        repo_args = ["--repo", state.github_repo] if state.github_repo else []
        existing = subprocess.run(
            ["gh", "pr", "list", *repo_args, "--head", state.branch_name, "--json", "number,url"],
            cwd=str(wt),
            env=author_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            items = json.loads(existing.stdout)
            if items:
                state.pr_number = int(items[0]["number"])
                state.pr_url = str(items[0]["url"])
                if state.mock_screenshot_paths and state.github_repo:
                    pinned = pin_mock_screenshots_to_pr(
                        pr_url_or_number=state.pr_url,
                        github_repo=state.github_repo,
                        worktree=wt,
                        mock_images=[Path(p) for p in state.mock_screenshot_paths if Path(p).is_file()],
                        actual_images=[Path(p) for p in state.actual_screenshot_paths if Path(p).is_file()] or None,
                        turn=turn,
                        author_gh_user=cfg.author_gh_user,
                        author_git_email=cfg.author_git_email,
                        author_env=author_env,
                    )
                    if pinned:
                        state.pinned_pr_image_urls = pinned
                return

        title_line = (cfg.feature_title or cfg.feature_request.splitlines()[0]).strip()[:72]
        pr_prefix = "fix" if cfg.regression_mode else "feat"
        pr_heading = "Automated Regression Fix PR" if cfg.regression_mode else "Automated Feature PR"
        pr_title = f"{pr_prefix}({state.shard}): {title_line}"
        source_line = f"**Prompt File:** `{Path(cfg.prompt_file).name}`\n\n" if cfg.prompt_file else ""
        spec_preview = _effective_shard_prompt(cfg, state.shard)[:2000]
        pr_body = (
            f"## {pr_heading} (`{cfg.author_gh_user}`)\n\n"
            f"{source_line}"
            f"### Prompt Specification\n{spec_preview}\n\n"
            f"### Turn 1 Summary\n{summary_note[:3000]}"
        )
        created = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                *repo_args,
                "--base",
                state.base_branch,
                "--head",
                state.branch_name,
                "--title",
                pr_title,
                "--body",
                pr_body,
            ],
            cwd=str(wt),
            env=author_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {created.stderr or created.stdout}")
        pr_url = created.stdout.strip().splitlines()[-1].strip()
        state.pr_url = pr_url
        view = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "number,url"],
            cwd=str(wt),
            env=author_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if view.returncode == 0 and view.stdout.strip():
            info = json.loads(view.stdout)
            state.pr_number = int(info["number"])
            state.pr_url = str(info["url"])

    if (state.pr_url or state.pr_number) and state.mock_screenshot_paths and state.github_repo:
        pinned = pin_mock_screenshots_to_pr(
            pr_url_or_number=state.pr_url or str(state.pr_number),
            github_repo=state.github_repo,
            worktree=wt,
            mock_images=[Path(p) for p in state.mock_screenshot_paths if Path(p).is_file()],
            actual_images=[Path(p) for p in state.actual_screenshot_paths if Path(p).is_file()] or None,
            turn=turn,
            author_gh_user=cfg.author_gh_user,
            author_git_email=cfg.author_git_email,
            author_env=author_env,
        )
        if pinned:
            state.pinned_pr_image_urls = pinned


def run_author_turn(
    *,
    cfg: PipelineConfig,
    state: ShardState,
    request_id: str,
    session_dir: Path,
) -> dict[str, Any]:
    turn = state.turns_used + 1
    wt = Path(state.worktree_path)

    if cfg.execution_mode == "dry_run":
        fake_env = build_scoped_git_gh_env(gh_user=cfg.author_gh_user, gh_token="dry-run-author-token")
        _configure_worktree_author_git(wt, cfg, fake_env)
        scenario = cfg.dry_run_scenarios.get(state.shard, {})
        fail_tests_until_turn = int(scenario.get("failTestsUntilTurn", 0))

        # Mutate a deterministic feature file + test file in the disposable git worktree
        feature_file = wt / "feature_impl.py"
        should_pass = turn > fail_tests_until_turn
        feature_file.write_text(
            f'"""Auto-generated feature turn {turn} by {cfg.author_gh_user}."""\n\n'
            f"TURN = {turn}\n"
            f"AUTHOR = {cfg.author_gh_user!r}\n\n"
            f"def compute_feature_value() -> int:\n"
            f"    return {42 if should_pass else -1}\n",
            encoding="utf-8",
        )
        tests_dir = wt / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_feature_impl.py").write_text(
            "from feature_impl import compute_feature_value\n\n"
            "def test_compute_feature_value():\n"
            "    assert compute_feature_value() == 42\n",
            encoding="utf-8",
        )
        summary = f"[dry_run] Authored turn {turn} as {cfg.author_gh_user} (model={cfg.author_model})"
        _ensure_committed_and_pr_opened(
            cfg=cfg,
            state=state,
            turn=turn,
            author_env=fake_env,
            summary_note=summary,
        )
        if state.pr_url is None:
            state.pr_number = 100 + hash(state.shard) % 900
            state.pr_url = f"https://github.com/example/{state.shard}/pull/{state.pr_number}"
        return {"turn": turn, "authorIdentity": cfg.author_gh_user, "summary": summary}

    # Live execution with strict `authorGhUser` credentials + Antigravity SDK
    author_token = resolve_gh_token(cfg.author_gh_user)
    author_env = build_scoped_git_gh_env(
        gh_user=cfg.author_gh_user,
        gh_token=author_token,
        git_email=cfg.author_git_email,
    )
    if (wt / "Cargo.toml").is_file():
        author_env["CARGO_TARGET_DIR"] = str(wt.parent / f".cargo-target-{state.shard}")
    _configure_worktree_author_git(wt, cfg, author_env)
    _hydrate_pr_review_context_if_needed(cfg=cfg, state=state, wt=wt, gh_env=author_env)

    # If resuming an existing PR branch on turn 1 that already has commits ahead of base
    # (and no pending test failure / review comments on the current HEAD commit yet), verify it directly
    # in `local_test_suite` -> `codex_review` before invoking another LLM author turn!
    spec = cfg.repos.get(state.shard)
    if (
        turn == 1
        and spec
        and spec.resume_branch
        and not spec.force_author_turn_one
        and state.last_test_passed is None
        and not state.pending_review_comments
    ):
        ahead_proc = _run_git(
            wt,
            ["rev-list", "--count", f"origin/{state.base_branch}..HEAD"],
            env=author_env,
            check=False,
        )
        ahead_count = int((ahead_proc.stdout or "0").strip() or "0") if ahead_proc.returncode == 0 else 0
        if ahead_count > 0:
            summary = f"Resumed existing branch `{state.branch_name}` ({ahead_count} commit(s) ahead of `{state.base_branch}`) for test & review verification."
            _ensure_committed_and_pr_opened(
                cfg=cfg,
                state=state,
                turn=turn,
                author_env=author_env,
                summary_note=summary,
            )
            return {"turn": turn, "authorIdentity": cfg.author_gh_user, "summary": summary}

    prompt = _build_author_prompt(cfg, state, turn)
    author_img_flags, _ = _collect_visual_attachment_args(state, include_actual=(turn > 1))
    repo_path_obj = Path(state.repo_path) if state.repo_path else None
    if cfg.author_provider == "codex":
        summary, new_session = _invoke_codex_author_agent(
            prompt=prompt,
            worktree=wt,
            model_name=cfg.author_model,
            agent_env=author_env,
            session_id=state.author_codex_session_id,
            image_flags=author_img_flags,
            repo_path=repo_path_obj,
        )
        if new_session:
            state.author_codex_session_id = new_session
    elif (
        os.environ.get("ANTIGRAVITY_USE_SDK") == "1"
        and any(
            author_env.get(k) or os.environ.get(k)
            for k in ("GEMINI_API_KEY", "GOOGLE_GEMINI_API_KEY", "GOOGLE_API_KEY")
        )
    ):
        conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"rippflow-pr-pipeline/{request_id}/{state.shard}"))
        summary = asyncio.run(
            _invoke_antigravity_agent(
                prompt=prompt,
                worktree=wt,
                session_dir=session_dir / state.shard,
                conversation_id=conv_id,
                model_name=cfg.author_model,
                agent_env=author_env,
            )
        )
    else:
        author_image_paths = [
            *state.mock_screenshot_paths,
            *(state.actual_screenshot_paths if turn > 1 else []),
        ]
        summary, _, new_conv_id = _invoke_agy_cli_agent(
            prompt=prompt,
            worktree=wt,
            model_name=cfg.author_model,
            agent_env=author_env,
            read_only=False,
            session_id=state.author_agy_conversation_id,
            image_paths=author_image_paths,
            repo_path=repo_path_obj,
        )
        if new_conv_id:
            state.author_agy_conversation_id = new_conv_id
    _ensure_committed_and_pr_opened(
        cfg=cfg,
        state=state,
        turn=turn,
        author_env=author_env,
        summary_note=summary,
    )
    return {"turn": turn, "authorIdentity": cfg.author_gh_user, "summary": summary[:1000]}


def run_codex_review_turn(
    *,
    cfg: PipelineConfig,
    state: ShardState,
) -> dict[str, Any]:
    """Runs Codex review under `reviewerGhUser` identity with persistent session + review ledger."""
    wt = Path(state.worktree_path)
    turn = state.turns_used

    if cfg.execution_mode == "dry_run":
        scenario = cfg.dry_run_scenarios.get(state.shard, {})
        comment_until_turn = int(scenario.get("requestChangesUntilTurn", 0))
        always_comment = bool(scenario.get("alwaysComment", False))
        if always_comment or turn <= comment_until_turn:
            return {
                "verdict": "comment",
                "reviewerIdentity": cfg.reviewer_gh_user,
                "summary": f"[dry_run] Turn {turn}: reviewer {cfg.reviewer_gh_user} requested refinements.",
                "comments": [
                    {
                        "path": "feature_impl.py",
                        "line": 5,
                        "severity": "P2",
                        "body": f"Turn {turn} review note: add edge-case handling before approval.",
                    }
                ],
                "ghAction": "commented",
                "headOid": "dry-run-oid",
                "sessionId": state.codex_session_id or "dry-run-codex-session",
            }
        return {
            "verdict": "approve",
            "reviewerIdentity": cfg.reviewer_gh_user,
            "summary": f"[dry_run] Turn {turn}: LGTM — approved by {cfg.reviewer_gh_user}.",
            "comments": [],
            "ghAction": "approved",
            "headOid": "dry-run-oid",
            "sessionId": state.codex_session_id or "dry-run-codex-session",
        }

    reviewer_token = resolve_gh_token(cfg.reviewer_gh_user)
    reviewer_env = build_scoped_git_gh_env(
        gh_user=cfg.reviewer_gh_user,
        gh_token=reviewer_token,
    )
    _hydrate_pr_review_context_if_needed(cfg=cfg, state=state, wt=wt, gh_env=reviewer_env)

    head_proc = _run_git(wt, ["rev-parse", "HEAD"], env=reviewer_env, check=False)
    head_oid = head_proc.stdout.strip() if head_proc.returncode == 0 else ""

    diff_proc = _run_git(wt, ["diff", "-U12", f"origin/{state.base_branch}...HEAD"], env=reviewer_env, check=False)
    diff_text = diff_proc.stdout.strip()
    if not diff_text:
        diff_text = _run_git(wt, ["diff", "-U12", f"{state.base_branch}...HEAD"], env=reviewer_env, check=False).stdout.strip()

    log_proc = _run_git(
        wt,
        ["log", "--stat", "-n", "10", f"origin/{state.base_branch}..HEAD"],
        env=reviewer_env,
        check=False,
    )
    commit_log_stat = log_proc.stdout.strip()
    if not commit_log_stat:
        commit_log_stat = _run_git(
            wt,
            ["log", "--stat", "-n", "10", f"{state.base_branch}..HEAD"],
            env=reviewer_env,
            check=False,
        ).stdout.strip()

    incremental_diff = ""
    if state.last_reviewed_commit and state.last_reviewed_commit != head_oid:
        inc_proc = _run_git(
            wt,
            ["diff", "-U12", f"{state.last_reviewed_commit}..HEAD"],
            env=reviewer_env,
            check=False,
        )
        if inc_proc.returncode == 0:
            incremental_diff = inc_proc.stdout.strip()

    spec_text = _effective_shard_prompt(cfg, state.shard)
    ledger_text = _format_prior_review_ledger(state.review_ledger, max_turns_shown=6)
    review_img_flags, review_img_manifest = _collect_visual_attachment_args(state, include_actual=True)

    prompt_parts: list[str] = [
        f"Review pull request changes on branch `{state.branch_name}` against `{state.base_branch}` "
        f"for repository `{state.shard}` (Turn {turn} of {cfg.max_turns}, HEAD `{head_oid[:7]}`).",
        f"Prompt Specification:\n{spec_text}",
    ]
    if cfg.regression_mode:
        prompt_parts.append(
            "CRITICAL REGRESSION & TDD REVIEW GATE:\n"
            "- This PR fixes a regression. Verify via `git log` / `git show` if needed that the patch addresses the true "
            "root-cause commit/mechanism (not a superficial symptom workaround) across ALL affected surfaces.\n"
            "- Verify the PR includes deterministic automated regression test(s) that directly exercise the broken path "
            "and would fail if the regression returned.\n"
            "- Verify the fix does NOT break the feature or behavior added by the commit that originally introduced the regression."
        )
    if review_img_manifest:
        has_actual = bool(state.actual_screenshot_paths)
        visual_gate_rule = (
            "CRITICAL VISUAL QA GATE:\n"
            "- Both `TARGET MOCK` reference image(s) AND `ACTUAL RENDERED UI` screenshot(s) from this commit are attached directly to this review turn.\n"
            "- If your harness uses `read_file` (or `view_file`) (e.g. `agy` CLI), call `read_file` (or `view_file`) on EVERY `TARGET MOCK` and `ACTUAL RENDERED UI` path listed above to inspect them visually.\n"
            "- Inspect every attached `ACTUAL RENDERED UI` screenshot pixel-by-pixel against the corresponding `TARGET MOCK` screenshot!\n"
            "- If the rendered UI deviates materially from the mock, "
            "you MUST return `verdict: \"comment\"` with concrete file/line findings citing the exact visual discrepancy!\n"
            "- Only return `verdict: \"approve\"` when BOTH code correctness AND visual design parity against the attached mock screenshots pass!"
            if has_actual
            else "VISUAL MOCK ATTACHED:\n"
            "- `TARGET MOCK` reference screenshot(s) are attached. If your harness uses `read_file` (or `view_file`), call `read_file` (or `view_file`) on each `TARGET MOCK` path listed above. Verify the UI layout/styling code matches the mock; if UI screenshot capture "
            "was requested in the specification (`RIPPFLOW_UI_SCREENSHOT_DIR`) and is missing, request it in your review comments."
        )
        prompt_parts.append(f"## Attached Visual QA Screenshots\n{review_img_manifest}\n\n{visual_gate_rule}")

    if ledger_text:
        prompt_parts.append(
            "## Prior Review Ledger (Your Earlier Requested Edits)\n"
            f"{ledger_text}\n\n"
            "Follow-up Review Protocol:\n"
            "1. First verify whether each open item from your latest review turn (including any visual parity findings) was properly resolved without regressing earlier fixes.\n"
            "2. Inspect any newly modified code AND newly captured `ACTUAL RENDERED UI` screenshots against the `TARGET MOCK` images.\n"
            "3. Do NOT invent cosmetic nitpicks beyond the specification/mock — "
            "only return `verdict: \"comment\"` if a prior requested edit remains unresolved, the rendered UI still fails visual parity with the mock, OR a genuine correctness/security/concurrency bug exists.\n"
            "4. When all prior requested edits and visual mock parity checks pass, return `verdict: \"approve\"` with `comments: []`."
        )
    else:
        prompt_parts.append(
            "Inspect the code, unit tests, and attached UI screenshots thoroughly for correctness, visual parity with the mock, concurrency hazards, security issues, "
            "edge cases, and test coverage.\n"
            "- Return `verdict: \"approve\"` with `comments: []` ONLY if there are zero actionable code or visual findings.\n"
            "- Otherwise return `verdict: \"comment\"` with concrete file/line findings."
        )

    if incremental_diff:
        prompt_parts.append(
            f"## Incremental Diff Since Last Reviewed Commit (`{state.last_reviewed_commit[:7]}..{head_oid[:7]}`)\n"
            f"```diff\n{incremental_diff[:16000]}\n```"
        )
    prompt_parts.append(f"## Full Branch Diff Preview (`{state.base_branch}...HEAD`)\n```diff\n{diff_text[:20000]}\n```")
    review_prompt = "\n\n".join(prompt_parts)

    session_id = state.reviewer_agy_conversation_id if cfg.reviewer_provider == "antigravity" else state.codex_session_id
    if cfg.reviewer_provider == "antigravity":
        review_imgs = [*state.mock_screenshot_paths, *state.actual_screenshot_paths]
        with tempfile.TemporaryDirectory(prefix="rippflow-agy-review-") as review_tmp:
            review_tmp_dir = Path(review_tmp)
            full_diff_path = review_tmp_dir / "pr_full_diff.patch"
            full_diff_sections: list[str] = []
            if commit_log_stat:
                full_diff_sections.append(
                    f"### Commit Log & Diffstat (`{state.base_branch}..HEAD`)\n{commit_log_stat}\n"
                )
            full_diff_sections.append(
                f"### Complete Untruncated Branch Diff (`{state.base_branch}...HEAD`)\n"
                + (diff_text if diff_text else "(no diff)")
            )
            full_diff_path.write_text("\n".join(full_diff_sections), encoding="utf-8")

            inc_diff_path: Path | None = None
            if incremental_diff:
                inc_diff_path = review_tmp_dir / "pr_incremental_diff.patch"
                inc_diff_path.write_text(incremental_diff, encoding="utf-8")

            readonly_guard = (
                "IMPORTANT — STRICT READ-ONLY REVIEWER CONSTRAINTS & FAST DIFF PROTOCOL:\n"
                "- You are running as a READ-ONLY code and Visual QA reviewer.\n"
                "- Do NOT edit, create, delete, stage, or commit any files in the workspace.\n"
                "- Do NOT run `gh` or `cargo` commands — the pipeline already ran tests/screenshots and posts your JSON verdict.\n"
                f"- COMPLETE UNTRUNCATED PR DIFF FILE: `{full_diff_path}` (with 12 lines of context and exact line numbers)."
                + (f"\n- INCREMENTAL DIFF SINCE LAST REVIEW: `{inc_diff_path}`." if inc_diff_path else "")
                + "\n- STEP 1: On your FIRST turn, call `read_file` in parallel on the diff file(s) above AND on EVERY `TARGET MOCK` and `ACTUAL RENDERED UI` PNG path listed below.\n"
                "- STEP 2: Do NOT page sequentially across unrelated 500-line sections of large files like `src/warp_shell.rs`. Only call `read_file` on a narrow, targeted line range if verifying a specific symbol touched by the diff strictly requires it.\n"
                "- STEP 3: As soon as you have verified code correctness and Visual QA screenshot parity, return ONLY valid JSON matching the required schema immediately.\n\n"
            )
            agy_review_prompt = readonly_guard + review_prompt
            resp_text, structured_output, new_conv_id = _invoke_agy_cli_agent(
                prompt=agy_review_prompt,
                worktree=wt,
                model_name=cfg.reviewer_model,
                agent_env=reviewer_env,
                read_only=True,
                session_id=session_id,
                json_schema=REVIEW_JSON_SCHEMA,
                image_paths=review_imgs,
                repo_path=None,
                extra_add_dirs=[str(review_tmp_dir)],
                timeout_sec=840,
            )
        dirty_check = _run_git(wt, ["status", "--porcelain"], env=reviewer_env, check=False)
        if dirty_check.returncode == 0 and dirty_check.stdout.strip():
            _run_git(wt, ["checkout", "--", "."], env=reviewer_env, check=False)
            _run_git(wt, ["clean", "-fd"], env=reviewer_env, check=False)
        if isinstance(structured_output, dict) and "verdict" in structured_output:
            review_data = structured_output
        else:
            review_data = _parse_review_json_fallback(resp_text)
        if new_conv_id:
            state.reviewer_agy_conversation_id = new_conv_id
            session_id = new_conv_id
    else:
        with tempfile.TemporaryDirectory(prefix="rippflow-codex-review-") as tmp:
            tmp_dir = Path(tmp)
            schema_path = tmp_dir / "review_schema.json"
            out_path = tmp_dir / "review_output.json"
            schema_path.write_text(json.dumps(REVIEW_JSON_SCHEMA, indent=2), encoding="utf-8")

            proc: subprocess.CompletedProcess[str] | None = None
            if session_id:
                resume_cmd = [
                    "codex",
                    "exec",
                    "resume",
                    "--skip-git-repo-check",
                    "-m",
                    cfg.reviewer_model,
                    *review_img_flags,
                    "--output-schema",
                    str(schema_path),
                    "-o",
                    str(out_path),
                    "--json",
                    session_id,
                    "-",
                ]
                proc = subprocess.run(
                    resume_cmd,
                    cwd=str(wt),
                    env=reviewer_env,
                    input=review_prompt,
                    capture_output=True,
                    text=True,
                    timeout=540,
                    check=False,
                )
                if proc.returncode != 0 or not out_path.is_file():
                    # Gracefully fall back to a fresh session (which still receives the full durable `review_ledger`)
                    proc = None
                    session_id = None

            if proc is None:
                fresh_cmd = [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "-C",
                    str(wt),
                    "-s",
                    "read-only",
                    "-m",
                    cfg.reviewer_model,
                    *review_img_flags,
                    "--output-schema",
                    str(schema_path),
                    "-o",
                    str(out_path),
                    "--json",
                    "-",
                ]
                proc = subprocess.run(
                    fresh_cmd,
                    cwd=str(wt),
                    env=reviewer_env,
                    input=review_prompt,
                    capture_output=True,
                    text=True,
                    timeout=540,
                    check=False,
                )
                if proc.returncode != 0 or not out_path.is_file():
                    raise RuntimeError(
                        f"codex exec review failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-2000:]}"
                    )

            detected_thread = _extract_codex_thread_id(proc.stdout)
            if detected_thread:
                session_id = detected_thread
            review_data = json.loads(out_path.read_text(encoding="utf-8"))

    comments = list(review_data.get("comments") or [])
    verdict = "approve" if (review_data.get("verdict") == "approve" and not comments) else "comment"
    summary = str(review_data.get("summary") or "").strip()

    # Post review to GitHub PR strictly as `reviewerGhUser`
    gh_action = "skipped_no_pr"
    if state.pr_url or state.pr_number:
        pr_ref = state.pr_url or str(state.pr_number)
        repo_args = ["--repo", state.github_repo] if state.github_repo and not state.pr_url else []
        reviewer_heading = "Automated Review" if cfg.reviewer_provider == "antigravity" else "Automated Codex Review"
        visual_badge = ""
        if state.mock_screenshot_paths or state.actual_screenshot_paths:
            visual_badge = (
                f"\n\n> **Visual QA Checked:** `{len(state.mock_screenshot_paths)}` pinned mock image(s) vs. "
                f"`{len(state.actual_screenshot_paths)}` rendered UI screenshot(s) (see PR description header)."
            )
        if verdict == "approve":
            body = f"### {reviewer_heading} (`{cfg.reviewer_model}`)\n\n{summary}{visual_badge}\n\nNo remaining findings — approving."
            rev = subprocess.run(
                ["gh", "pr", "review", pr_ref, *repo_args, "--approve", "--body", body],
                cwd=str(wt),
                env=reviewer_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if rev.returncode != 0:
                raise RuntimeError(f"gh pr review --approve failed: {rev.stderr or rev.stdout}")
            gh_action = "approved"
        else:
            bullets = "\n".join(
                f"- **[{c['severity']}]** `{c['path']}:{c['line']}` — {c['body']}"
                for c in comments
            )
            turn_note = (
                f"*(Turn {turn}/{cfg.max_turns} — escalating to human review)*"
                if turn >= cfg.max_turns
                else f"*(Turn {turn}/{cfg.max_turns} — returning to `{cfg.author_gh_user}`)*"
            )
            body = f"### {reviewer_heading} (`{cfg.reviewer_model}`) {turn_note}\n\n{summary}{visual_badge}\n\n{bullets}"
            rev = subprocess.run(
                ["gh", "pr", "review", pr_ref, *repo_args, "--comment", "--body", body],
                cwd=str(wt),
                env=reviewer_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if rev.returncode != 0:
                raise RuntimeError(f"gh pr review --comment failed: {rev.stderr or rev.stdout}")
            gh_action = "commented"

    return {
        "verdict": verdict,
        "reviewerIdentity": cfg.reviewer_gh_user,
        "summary": summary,
        "comments": comments,
        "ghAction": gh_action,
        "headOid": head_oid,
        "sessionId": session_id,
    }
