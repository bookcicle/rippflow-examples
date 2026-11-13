"""Configuration, durable local run-state store, and dual GitHub identity helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


DEFAULT_MAX_TURNS = 15


def load_dotenv_files(*candidate_dirs: Path) -> dict[str, str]:
    """Loads `.env` key-value pairs from candidate directories without clobbering existing `os.environ`."""
    loaded: dict[str, str] = {}
    for directory in candidate_dirs:
        env_path = directory / ".env"
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key:
                loaded[key] = val
                os.environ.setdefault(key, val)
    return loaded


def _resolve_default_workspace(workflow_dir: Path) -> Path:
    """Finds the workspace directory containing sibling git repositories (handling nested `examples/<name>` layout)."""
    env_ws = os.environ.get("WORKSPACE_DIR")
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    # If nested inside `<workspace>/rippflow-examples/examples/<workflow>`, climb to `<workspace>`.
    if workflow_dir.parent.name == "examples" and (workflow_dir.parent.parent / ".git").exists():
        return workflow_dir.parent.parent.parent.resolve()
    return workflow_dir.parent.resolve()


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:max_len].strip("-") or "feature")


def extract_prompt_title(prompt_text: str, prompt_file: Path | None = None, max_len: int = 72) -> str:
    """Extracts a clean human-readable title from a full Markdown/text Codex prompt file."""
    for raw_line in prompt_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```") or line.startswith("---"):
            continue
        cleaned = re.sub(r"^#+\s*", "", line).strip()
        if cleaned:
            return cleaned[:max_len].strip()
    if prompt_file is not None:
        return prompt_file.stem[:max_len]
    return "implement requested feature"


def resolve_asset_path(raw_path: str, *, search_roots: list[Path], label: str = "file") -> Path:
    """Resolves a file or directory path against explicit path, `~`, or candidate workspace/workflow directories."""
    cand = Path(raw_path).expanduser()
    if cand.is_absolute():
        if not cand.exists():
            raise FileNotFoundError(f"{label} does not exist: {cand}")
        return cand.resolve()

    for root in search_roots:
        resolved = (root / cand).resolve()
        if resolved.exists():
            return resolved
    raise FileNotFoundError(
        f"{label} '{raw_path}' not found (searched: {', '.join(str(r) for r in search_roots)})"
    )


def resolve_prompt_file_path(raw_path: str, *, search_roots: list[Path]) -> Path:
    """Resolves `promptFile` against explicit path, `~`, or candidate workspace/workflow directories."""
    resolved = resolve_asset_path(raw_path, search_roots=search_roots, label="promptFile")
    if not resolved.is_file():
        raise FileNotFoundError(f"promptFile does not exist: {resolved}")
    return resolved


@dataclass
class RepoSpec:
    name: str
    path: str
    base_branch: str = "main"
    resume_branch: str | None = None
    force_author_turn_one: bool = False
    test_command: list[str] | None = None
    screenshot_command: list[str] | None = None
    github_repo: str | None = None
    companion_repos: list[str] = field(default_factory=list)
    prompt_file: str | None = None
    mock_html: str | None = None
    mock_screenshots: list[str] = field(default_factory=list)


@dataclass
class PipelineConfig:
    feature_request: str
    workspace_dir: str
    repos: dict[str, RepoSpec]
    prompt_file: str | None = None
    mock_html: str | None = None
    mock_screenshots: list[str] = field(default_factory=list)
    feature_title: str = ""
    author_provider: str = "antigravity"  # "antigravity" | "codex"
    author_gh_user: str = "bot-author"
    reviewer_gh_user: str = "human-reviewer"
    author_git_email: str | None = None
    author_signing_key: str | None = None
    author_model: str = "gemini-3.8-flash"
    reviewer_model: str = "gpt-6.1-sol"
    max_turns: int = DEFAULT_MAX_TURNS
    branch_prefix: str = "ai/feature"
    execution_mode: str = "live"  # "live" | "dry_run"
    enable_os_notifications: bool = True
    dry_run_scenarios: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        *,
        default_workspace: Path,
        workflow_dir: Path | None = None,
    ) -> "PipelineConfig":
        wf_root = (workflow_dir or Path.cwd()).resolve()
        load_dotenv_files(wf_root, default_workspace, Path.cwd())

        ws_path = (
            Path(
                raw.get("workspaceDir")
                or raw.get("workspace_dir")
                or os.environ.get("WORKSPACE_DIR")
                or default_workspace
            )
            .expanduser()
            .resolve()
        )
        ws = str(ws_path)
        search_roots = [wf_root, ws_path, Path.cwd().resolve()]

        raw_prompt_file = (
            raw.get("promptFile")
            or raw.get("prompt_file")
            or raw.get("codexPromptFile")
            or raw.get("codex_prompt_file")
            or raw.get("featureRequestFile")
            or raw.get("feature_request_file")
        )
        resolved_prompt_path: Path | None = None
        prompt_file_str: str | None = None
        if raw_prompt_file:
            resolved_prompt_path = resolve_prompt_file_path(str(raw_prompt_file).strip(), search_roots=search_roots)
            prompt_file_str = str(resolved_prompt_path)
            file_content = resolved_prompt_path.read_text(encoding="utf-8").strip()
            if not file_content:
                raise ValueError(f"promptFile is empty: {resolved_prompt_path}")
        else:
            file_content = ""

        inline_request = str(raw.get("featureRequest") or raw.get("feature_request") or "").strip()
        if file_content and inline_request:
            feature_request = f"{file_content}\n\n## Additional Context\n{inline_request}"
        else:
            feature_request = file_content or inline_request

        if not feature_request:
            raise ValueError("input payload must provide 'promptFile' (path to prompt file) or non-empty 'featureRequest'")

        explicit_title = str(raw.get("featureTitle") or raw.get("feature_title") or "").strip()
        feature_title = explicit_title or extract_prompt_title(feature_request, resolved_prompt_path)

        default_turns = int(os.environ.get("MAX_TURNS") or DEFAULT_MAX_TURNS)
        max_turns = int(raw.get("maxTurns", raw.get("max_turns", default_turns)))
        if max_turns < 1 or max_turns > 50:
            raise ValueError(f"maxTurns must be between 1 and 50 (got {max_turns})")

        raw_mock_html = raw.get("mockHtml") or raw.get("mock_html") or raw.get("mockFile") or raw.get("mock_file")
        mock_html_str = (
            str(resolve_asset_path(str(raw_mock_html).strip(), search_roots=search_roots, label="mockHtml"))
            if raw_mock_html
            else None
        )

        raw_mock_shots = raw.get("mockScreenshots") or raw.get("mock_screenshots") or []
        if isinstance(raw_mock_shots, str):
            raw_mock_shots = [raw_mock_shots]
        mock_screenshots_list = [
            str(resolve_asset_path(str(item).strip(), search_roots=search_roots, label="mockScreenshots"))
            for item in raw_mock_shots
            if str(item).strip()
        ]

        def _parse_repo_entry(name: str, entry: dict[str, Any]) -> RepoSpec:
            rpath = str(Path(entry.get("path") or (Path(ws) / name)).resolve())
            repo_roots = [Path(rpath), *search_roots]
            tcmd = entry.get("testCommand") or entry.get("test_command")
            if isinstance(tcmd, str):
                tcmd = ["sh", "-lc", tcmd]
            scmd = entry.get("screenshotCommand") or entry.get("screenshot_command")
            if isinstance(scmd, str):
                scmd = ["sh", "-lc", scmd]
            companions = entry.get("companionRepos") or entry.get("companion_repos") or []
            repo_pf = entry.get("promptFile") or entry.get("prompt_file")
            resolved_repo_pf = (
                str(resolve_prompt_file_path(str(repo_pf).strip(), search_roots=repo_roots))
                if repo_pf
                else None
            )
            repo_mh = entry.get("mockHtml") or entry.get("mock_html")
            resolved_repo_mh = (
                str(resolve_asset_path(str(repo_mh).strip(), search_roots=repo_roots, label="mockHtml"))
                if repo_mh
                else None
            )
            repo_ms = entry.get("mockScreenshots") or entry.get("mock_screenshots") or []
            if isinstance(repo_ms, str):
                repo_ms = [repo_ms]
            resolved_repo_ms = [
                str(resolve_asset_path(str(x).strip(), search_roots=repo_roots, label="mockScreenshots"))
                for x in repo_ms
                if str(x).strip()
            ]
            force_author = bool(
                entry.get(
                    "forceAuthorTurnOne",
                    entry.get(
                        "force_author_turn_one",
                        raw.get("forceAuthorTurnOne", raw.get("force_author_turn_one", False)),
                    ),
                )
            )
            return RepoSpec(
                name=name,
                path=rpath,
                base_branch=str(entry.get("baseBranch") or "main"),
                resume_branch=entry.get("resumeBranch") or entry.get("resume_branch"),
                force_author_turn_one=force_author,
                test_command=list(tcmd) if tcmd else None,
                screenshot_command=list(scmd) if scmd else None,
                github_repo=entry.get("githubRepo") or entry.get("github_repo"),
                companion_repos=[str(c) for c in companions],
                prompt_file=resolved_repo_pf,
                mock_html=resolved_repo_mh,
                mock_screenshots=resolved_repo_ms,
            )

        raw_repos = raw.get("repos") or []
        repos: dict[str, RepoSpec] = {}
        if isinstance(raw_repos, list):
            for entry in raw_repos:
                if isinstance(entry, str):
                    repo_path = str((Path(ws) / entry).resolve())
                    repos[entry] = RepoSpec(name=entry, path=repo_path)
                elif isinstance(entry, dict):
                    name = str(entry["name"])
                    repos[name] = _parse_repo_entry(name, entry)
        elif isinstance(raw_repos, dict):
            for name, entry in raw_repos.items():
                repos[name] = _parse_repo_entry(str(name), entry)

        author_provider = str(
            raw.get("authorProvider")
            or raw.get("author_provider")
            or os.environ.get("AUTHOR_PROVIDER")
            or "antigravity"
        ).strip().lower()
        if author_provider not in {"antigravity", "gemini", "codex"}:
            raise ValueError(f"Unsupported authorProvider '{author_provider}' (expected 'antigravity' or 'codex')")

        return cls(
            feature_request=feature_request,
            workspace_dir=ws,
            repos=repos,
            prompt_file=prompt_file_str,
            mock_html=mock_html_str,
            mock_screenshots=mock_screenshots_list,
            feature_title=feature_title,
            author_provider="codex" if author_provider == "codex" else "antigravity",
            author_gh_user=str(raw.get("authorGhUser") or os.environ.get("AUTHOR_GH_USER") or "bot-author"),
            reviewer_gh_user=str(raw.get("reviewerGhUser") or os.environ.get("REVIEWER_GH_USER") or "human-reviewer"),
            author_git_email=raw.get("authorGitEmail") or os.environ.get("AUTHOR_GIT_EMAIL"),
            author_signing_key=raw.get("authorSigningKey") or os.environ.get("AUTHOR_SIGNING_KEY"),
            author_model=str(raw.get("authorModel") or os.environ.get("AUTHOR_MODEL") or "gemini-3.8-flash"),
            reviewer_model=str(raw.get("reviewerModel") or os.environ.get("REVIEWER_MODEL") or "gpt-6.1-sol"),
            max_turns=max_turns,
            branch_prefix=str(raw.get("branchPrefix") or os.environ.get("BRANCH_PREFIX") or "ai/feature"),
            execution_mode=str(raw.get("executionMode") or os.environ.get("PIPELINE_EXECUTION_MODE") or "live"),
            enable_os_notifications=(
                bool(raw["enableOsNotifications"])
                if "enableOsNotifications" in raw
                else os.environ.get("ENABLE_OS_NOTIFICATIONS", "true").strip().lower() not in ("0", "false", "no")
            ),
            dry_run_scenarios=dict(raw.get("dryRunScenarios") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["repos"] = {k: asdict(v) for k, v in self.repos.items()}
        return data


@dataclass
class ShardState:
    shard: str
    repo_path: str
    worktree_path: str
    branch_name: str
    base_branch: str
    github_repo: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    turns_used: int = 0
    status: str = "initialized"  # initialized | authored | tests_passed | approved | escalated
    escalation_reason: str | None = None
    last_test_passed: bool | None = None
    last_test_output: str = ""
    last_review_verdict: str | None = None
    last_review_summary: str = ""
    pending_review_comments: list[dict[str, Any]] = field(default_factory=list)
    author_codex_session_id: str | None = None
    codex_session_id: str | None = None
    last_reviewed_commit: str | None = None
    review_ledger: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    human_resolution: dict[str, Any] | None = None
    mock_screenshot_paths: list[str] = field(default_factory=list)
    actual_screenshot_paths: list[str] = field(default_factory=list)
    pinned_pr_image_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ShardState":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in raw.items() if k in known_fields}
        return cls(**filtered)


class RunStore:
    """File-backed state directory for a single Rippflow `request_id`."""

    def __init__(self, repo_root: Path, request_id: str) -> None:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", request_id)
        self.repo_root = repo_root
        self.request_id = request_id
        self.run_dir = repo_root / ".runs" / safe_id
        self.shards_dir = self.run_dir / "shards"
        self.worktrees_dir = self.run_dir / "worktrees"
        self.escalations_dir = self.run_dir / "escalations"
        self.sessions_dir = self.run_dir / "agy_sessions"

    def ensure_dirs(self) -> None:
        for path in (
            self.run_dir,
            self.shards_dir,
            self.worktrees_dir,
            self.escalations_dir,
            self.sessions_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def save_config(self, cfg: PipelineConfig) -> None:
        self.ensure_dirs()
        (self.run_dir / "config.json").write_text(
            json.dumps(cfg.to_dict(), indent=2),
            encoding="utf-8",
        )

    def load_config(self) -> PipelineConfig:
        raw = json.loads((self.run_dir / "config.json").read_text(encoding="utf-8"))
        repos = {k: RepoSpec(**v) for k, v in raw.pop("repos", {}).items()}
        cfg = PipelineConfig(**raw, repos=repos)
        return cfg

    def shard_file(self, shard: str) -> Path:
        safe_shard = re.sub(r"[^a-zA-Z0-9_.-]", "_", shard)
        return self.shards_dir / f"{safe_shard}.json"

    def save_shard(self, state: ShardState) -> None:
        self.ensure_dirs()
        tmp = self.shard_file(state.shard).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.shard_file(state.shard))

    def load_shard(self, shard: str) -> ShardState:
        data = json.loads(self.shard_file(shard).read_text(encoding="utf-8"))
        return ShardState.from_dict(data)

    def load_all_shards(self) -> list[ShardState]:
        if not self.shards_dir.exists():
            return []
        out = []
        for path in sorted(self.shards_dir.glob("*.json")):
            out.append(ShardState.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return out


def resolve_gh_token(gh_user: str) -> str:
    """Fetches a scoped GitHub token from `gh auth token --user <gh_user>` without mutating global auth."""
    env_override = os.environ.get(f"GH_TOKEN_{re.sub(r'[^A-Za-z0-9]', '_', gh_user).upper()}")
    if env_override:
        return env_override.strip()

    proc = subprocess.run(
        ["gh", "auth", "token", "--user", gh_user],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(
            f"Unable to resolve GitHub token for account '{gh_user}' via `gh auth token --user {gh_user}`: {detail}"
        )
    return proc.stdout.strip()


def build_scoped_git_gh_env(
    *,
    gh_user: str,
    gh_token: str,
    git_email: str | None = None,
) -> dict[str, str]:
    """Builds a subprocess environment strictly isolated to a single GitHub identity."""
    env = os.environ.copy()
    for scrubbed in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "RIPPFLOW_CALLBACK_SIGNING_KEY"):
        env.pop(scrubbed, None)
    email = git_email or f"{gh_user}@users.noreply.github.com"
    env["GH_TOKEN"] = gh_token
    env["GIT_AUTHOR_NAME"] = gh_user
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = gh_user
    env["GIT_COMMITTER_EMAIL"] = email
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def detect_github_repo_slug(repo_path: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def detect_default_test_command(worktree_path: Path) -> list[str]:
    if (worktree_path / "Cargo.toml").is_file():
        return ["cargo", "test", "--locked"]
    if (worktree_path / "package.json").is_file():
        return ["npm", "test"]
    if (worktree_path / "pyproject.toml").is_file() or (worktree_path / "pytest.ini").is_file() or (worktree_path / "tests").is_dir():
        return ["python3", "-m", "pytest", "-q", "--rootdir", "."]
    if (worktree_path / "go.mod").is_file():
        return ["go", "test", "./..."]
    return ["python3", "-m", "unittest", "discover"]
