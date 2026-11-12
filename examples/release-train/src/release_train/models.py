"""Configuration, `.env` discovery, runtime-tier gating (`RIPPFLOW_ENV`), and durable state store."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
import os
from pathlib import Path
import re
from typing import Any


def detect_rippflow_env() -> str:
    """Reads `RIPPFLOW_ENV` / `rippflow_env` injected by Rippflow runtime (`local`, `device`, `edge`, `cloud`)."""
    raw = (os.environ.get("RIPPFLOW_ENV") or os.environ.get("rippflow_env") or "").strip().lower()
    if raw in {"local", "device", "edge", "cloud"}:
        return raw
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("AWS_EXECUTION_ENV"):
        return "cloud"
    return "local"


def is_cloud_env(rippflow_env: str | None = None) -> bool:
    return (rippflow_env or detect_rippflow_env()).strip().lower() == "cloud"


def load_dotenv_files(*candidate_dirs: Path) -> dict[str, str]:
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


def resolve_default_workspace(workflow_dir: Path) -> Path:
    env_ws = os.environ.get("WORKSPACE_DIR")
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    if workflow_dir.parent.name == "examples" and (workflow_dir.parent.parent / ".git").exists():
        return workflow_dir.parent.parent.parent.resolve()
    return workflow_dir.parent.resolve()


@dataclass
class ReleaseRepoSpec:
    name: str
    path: str
    base_branch: str = "main"
    version_file: str | None = None
    pinned_sha_file: str | None = None
    docs_sync_target: str | None = None
    verify_command: list[str] | None = None
    benchmark_command: list[str] | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ReleaseConfig:
    release_version: str
    workspace_dir: str
    repos: dict[str, ReleaseRepoSpec]
    publish_order: list[str] = field(default_factory=list)
    rippflow_env: str = "local"
    ai_changelog_enabled: bool = True
    changelog_provider: str = "auto"  # "auto" | "gemini" | "codex" | "deterministic"
    gemini_model: str = "gemini-3.8-flash"
    codex_model: str = "gpt-6.1-sol"
    execution_mode: str = "dry_run"  # "dry_run" (writes staged tags/commits in worktrees) | "live"
    auto_approve_gate: bool = False
    slack_webhook_url: str | None = None
    email_webhook_url: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, workflow_dir: Path) -> "ReleaseConfig":
        load_dotenv_files(workflow_dir, workflow_dir.parent, workflow_dir.parent.parent)
        rt_env = detect_rippflow_env()
        cloud_mode = is_cloud_env(rt_env)

        release_version = str(
            raw.get("releaseVersion")
            or raw.get("release_version")
            or os.environ.get("RELEASE_VERSION")
            or "0.1.0"
        ).strip()
        if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._-]+)?$", release_version):
            raise ValueError(f"Invalid semantic releaseVersion '{release_version}'")

        ws_root = Path(
            raw.get("workspaceDir")
            or raw.get("workspace_dir")
            or resolve_default_workspace(workflow_dir)
        ).expanduser().resolve()

        # Crucial invariant: when `RIPPFLOW_ENV == "cloud"`, Codex/Gemini CLIs & SDKs are ALWAYS
        # flagged off so cloud execution has ZERO dependency on local AI agent installations!
        if cloud_mode:
            ai_enabled = False
            provider = "deterministic"
        else:
            manifest_default = os.environ.get("AI_CHANGELOG_DEFAULT", "enabled").strip().lower() != "disabled"
            env_flag = os.environ.get("ENABLE_AI_CHANGELOG", "auto").strip().lower()
            if "enableAiChangelog" in raw:
                ai_enabled = bool(raw["enableAiChangelog"])
            elif env_flag in {"0", "false", "no", "disabled"}:
                ai_enabled = False
            elif env_flag in {"1", "true", "yes", "enabled"}:
                ai_enabled = True
            else:
                ai_enabled = manifest_default
            provider = str(
                raw.get("changelogProvider")
                or os.environ.get("CHANGELOG_PROVIDER")
                or ("auto" if ai_enabled else "deterministic")
            ).strip().lower()

        raw_repos = raw.get("repos") or []
        repos: dict[str, ReleaseRepoSpec] = {}
        entries = (
            [{"name": k, **v} if isinstance(v, dict) else {"name": k} for k, v in raw_repos.items()]
            if isinstance(raw_repos, dict)
            else raw_repos
        )
        for item in entries:
            if isinstance(item, str):
                repos[item] = ReleaseRepoSpec(name=item, path=str((ws_root / item).resolve()))
            elif isinstance(item, dict):
                name = str(item["name"])
                rpath = str(Path(item.get("path") or (ws_root / name)).expanduser().resolve())
                vcmd = item.get("verifyCommand") or item.get("verify_command")
                if isinstance(vcmd, str):
                    vcmd = ["sh", "-lc", vcmd]
                bcmd = item.get("benchmarkCommand") or item.get("benchmark_command")
                if isinstance(bcmd, str):
                    bcmd = ["sh", "-lc", bcmd]
                repos[name] = ReleaseRepoSpec(
                    name=name,
                    path=rpath,
                    base_branch=str(item.get("baseBranch") or "main"),
                    version_file=item.get("versionFile") or item.get("version_file"),
                    pinned_sha_file=item.get("pinnedShaFile") or item.get("pinned_sha_file"),
                    docs_sync_target=item.get("docsSyncTarget") or item.get("docs_sync_target"),
                    verify_command=list(vcmd) if vcmd else None,
                    benchmark_command=list(bcmd) if bcmd else None,
                    depends_on=[str(d) for d in (item.get("dependsOn") or item.get("depends_on") or [])],
                )

        pub_order = [str(x) for x in (raw.get("publishOrder") or raw.get("publish_order") or list(repos.keys()))]
        for rname in repos:
            if rname not in pub_order:
                pub_order.append(rname)

        return cls(
            release_version=release_version,
            workspace_dir=str(ws_root),
            repos=repos,
            publish_order=pub_order,
            rippflow_env=rt_env,
            ai_changelog_enabled=ai_enabled,
            changelog_provider=provider,
            gemini_model=str(raw.get("geminiModel") or os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"),
            codex_model=str(raw.get("codexModel") or os.environ.get("CODEX_MODEL") or "gpt-6.1-sol"),
            execution_mode=str(raw.get("executionMode") or os.environ.get("RELEASE_EXECUTION_MODE") or "dry_run"),
            auto_approve_gate=bool(raw.get("autoApproveGate", False)),
            slack_webhook_url=raw.get("slackWebhookUrl") or os.environ.get("SLACK_WEBHOOK_URL") or None,
            email_webhook_url=raw.get("emailWebhookUrl") or os.environ.get("EMAIL_WEBHOOK_URL") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["repos"] = {k: asdict(v) for k, v in self.repos.items()}
        return data


@dataclass
class ShardReleaseState:
    shard: str
    repo_path: str
    worktree_path: str
    base_branch: str
    head_sha_before: str = ""
    head_sha_after: str = ""
    commits_since_last_tag: list[str] = field(default_factory=list)
    drift_detected: bool = False
    pinned_sha_drifts: dict[str, dict[str, str]] = field(default_factory=dict)
    bumped_files: list[str] = field(default_factory=list)
    changelog_markdown: str = ""
    changelog_generator: str = "deterministic"
    verify_passed: bool | None = None
    verify_output: str = ""
    artifact_checksums: dict[str, str] = field(default_factory=dict)
    benchmark_summary: str = ""
    published_tag: str | None = None
    status: str = "detected"  # detected | bumped | verified | published

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ShardReleaseState":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in known})


class ReleaseRunStore:
    """File-backed state store rooted in `/tmp/rippflow-release-train` on `cloud` Lambda or `.runs` locally."""

    def __init__(self, workflow_dir: Path, request_id: str, *, rippflow_env: str | None = None) -> None:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", request_id)
        rt = (rippflow_env or detect_rippflow_env()).strip().lower()
        explicit_state_dir = os.environ.get("RIPPFLOW_STATE_DIR")
        if explicit_state_dir:
            base_runs = Path(explicit_state_dir)
        elif rt == "cloud" and not os.access(workflow_dir, os.W_OK):
            base_runs = Path("/tmp/rippflow-release-train/.runs")
        else:
            base_runs = workflow_dir / ".runs"
        self.workflow_dir = workflow_dir
        self.request_id = request_id
        self.run_dir = base_runs / safe_id
        self.shards_dir = self.run_dir / "shards"
        self.worktrees_dir = self.run_dir / "worktrees"
        self.signoff_dir = self.run_dir / "signoff"

    def ensure_dirs(self) -> None:
        for d in (self.run_dir, self.shards_dir, self.worktrees_dir, self.signoff_dir):
            d.mkdir(parents=True, exist_ok=True)

    def save_config(self, cfg: ReleaseConfig) -> None:
        self.ensure_dirs()
        (self.run_dir / "config.json").write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")

    def load_config(self) -> ReleaseConfig:
        raw = json.loads((self.run_dir / "config.json").read_text(encoding="utf-8"))
        repos = {k: ReleaseRepoSpec(**v) for k, v in raw.pop("repos", {}).items()}
        return ReleaseConfig(**raw, repos=repos)

    def shard_file(self, shard: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", shard)
        return self.shards_dir / f"{safe}.json"

    def save_shard(self, state: ShardReleaseState) -> None:
        self.ensure_dirs()
        tmp = self.shard_file(state.shard).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.shard_file(state.shard))

    def load_shard(self, shard: str) -> ShardReleaseState:
        return ShardReleaseState.from_dict(json.loads(self.shard_file(shard).read_text(encoding="utf-8")))

    def load_all_shards(self) -> list[ShardReleaseState]:
        if not self.shards_dir.exists():
            return []
        return [
            ShardReleaseState.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(self.shards_dir.glob("*.json"))
        ]
