"""Configuration, `.env` discovery, runtime-tier gating (`RIPPFLOW_ENV`), and durable state store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
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


def _env_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on", "enabled"}:
        return True
    if s in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


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
class HygieneOptions:
    clean_stale_worktrees: bool = True
    stale_worktree_hours: float = 24.0
    delete_cargo_targets: bool = True
    extra_cargo_target_dirs: list[str] = field(default_factory=list)
    clean_docker_images: bool = True
    docker_prune_until: str = "72h"


@dataclass
class NightwatchRepoSpec:
    name: str
    path: str
    base_branch: str = "main"
    audit_command: list[str] | None = None
    outdated_command: list[str] | None = None
    warnings_command: list[str] | None = None
    test_command: list[str] | None = None
    upgrade_command: list[str] | None = None
    benchmark_command: list[str] | None = None


@dataclass
class NightwatchConfig:
    workspace_dir: str
    repos: dict[str, NightwatchRepoSpec]
    hygiene: HygieneOptions = field(default_factory=HygieneOptions)
    rippflow_env: str = "local"
    execution_mode: str = "dry_run"  # "dry_run" | "live"
    ai_upgrader_enabled: bool = True
    upgrader_provider: str = "auto"  # "auto" | "gemini" | "codex" | "deterministic"
    gemini_model: str = "gemini-4-hydrogen-medium"
    codex_model: str = "gpt-6.1-sol"
    flaky_test_runs: int = 2
    max_benchmark_regression_pct: float = 5.0
    gh_user: str | None = None
    git_email: str | None = None
    branch_prefix: str = "nightwatch/deps"
    enable_os_notifications: bool = True
    slack_webhook_url: str | None = None
    email_webhook_url: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, workflow_dir: Path) -> "NightwatchConfig":
        load_dotenv_files(workflow_dir, workflow_dir.parent, workflow_dir.parent.parent)
        rt_env = detect_rippflow_env()
        cloud_mode = is_cloud_env(rt_env)

        ws_root = Path(
            raw.get("workspaceDir")
            or raw.get("workspace_dir")
            or resolve_default_workspace(workflow_dir)
        ).expanduser().resolve()

        hyg_raw = raw.get("hygiene") if isinstance(raw.get("hygiene"), dict) else {}
        hygiene = HygieneOptions(
            clean_stale_worktrees=_env_bool(
                hyg_raw.get("cleanStaleWorktrees", raw.get("cleanStaleWorktrees")),
                _env_bool(os.environ.get("CLEAN_STALE_WORKTREES"), True),
            ),
            stale_worktree_hours=float(
                hyg_raw.get("staleWorktreeHours")
                or raw.get("staleWorktreeHours")
                or os.environ.get("STALE_WORKTREE_HOURS")
                or 24.0
            ),
            delete_cargo_targets=_env_bool(
                hyg_raw.get("deleteCargoTargets", raw.get("deleteCargoTargets")),
                _env_bool(os.environ.get("DELETE_CARGO_TARGETS"), True),
            ),
            extra_cargo_target_dirs=[
                str(Path(p).expanduser().resolve())
                for p in (hyg_raw.get("extraCargoTargetDirs") or raw.get("extraCargoTargetDirs") or [])
            ],
            clean_docker_images=_env_bool(
                hyg_raw.get("cleanDockerImages", raw.get("cleanDockerImages")),
                _env_bool(os.environ.get("CLEAN_DOCKER_IMAGES"), True),
            ),
            docker_prune_until=str(
                hyg_raw.get("dockerPruneUntil")
                or raw.get("dockerPruneUntil")
                or os.environ.get("DOCKER_PRUNE_UNTIL")
                or "72h"
            ),
        )

        # Crucial cloud invariant: when `RIPPFLOW_ENV == "cloud"`, Codex and Gemini are ALWAYS flagged off!
        if cloud_mode:
            ai_enabled = False
            provider = "deterministic"
        else:
            manifest_default = os.environ.get("AI_UPGRADER_DEFAULT", "enabled").strip().lower() != "disabled"
            env_flag = os.environ.get("ENABLE_AI_UPGRADER", "auto").strip().lower()
            if "enableAiUpgrader" in raw:
                ai_enabled = bool(raw["enableAiUpgrader"])
            elif env_flag in {"0", "false", "no", "disabled"}:
                ai_enabled = False
            elif env_flag in {"1", "true", "yes", "enabled"}:
                ai_enabled = True
            else:
                ai_enabled = manifest_default
            provider = str(
                raw.get("upgraderProvider")
                or os.environ.get("UPGRADER_PROVIDER")
                or ("auto" if ai_enabled else "deterministic")
            ).strip().lower()

        def _norm_cmd(val: Any) -> list[str] | None:
            if not val:
                return None
            if isinstance(val, str):
                return ["sh", "-lc", val]
            return [str(x) for x in val]

        raw_repos = raw.get("repos") or []
        repos: dict[str, NightwatchRepoSpec] = {}
        entries = (
            [{"name": k, **v} if isinstance(v, dict) else {"name": k} for k, v in raw_repos.items()]
            if isinstance(raw_repos, dict)
            else raw_repos
        )
        for item in entries:
            if isinstance(item, str):
                repos[item] = NightwatchRepoSpec(name=item, path=str((ws_root / item).resolve()))
            elif isinstance(item, dict):
                name = str(item["name"])
                rpath = str(Path(item.get("path") or (ws_root / name)).expanduser().resolve())
                repos[name] = NightwatchRepoSpec(
                    name=name,
                    path=rpath,
                    base_branch=str(item.get("baseBranch") or "main"),
                    audit_command=_norm_cmd(item.get("auditCommand") or item.get("audit_command")),
                    outdated_command=_norm_cmd(item.get("outdatedCommand") or item.get("outdated_command")),
                    warnings_command=_norm_cmd(item.get("warningsCommand") or item.get("warnings_command")),
                    test_command=_norm_cmd(item.get("testCommand") or item.get("test_command")),
                    upgrade_command=_norm_cmd(item.get("upgradeCommand") or item.get("upgrade_command")),
                    benchmark_command=_norm_cmd(item.get("benchmarkCommand") or item.get("benchmark_command")),
                )

        return cls(
            workspace_dir=str(ws_root),
            repos=repos,
            hygiene=hygiene,
            rippflow_env=rt_env,
            execution_mode=str(
                raw.get("executionMode") or os.environ.get("NIGHTWATCH_EXECUTION_MODE") or "dry_run"
            ).strip(),
            ai_upgrader_enabled=ai_enabled,
            upgrader_provider=provider,
            gemini_model=str(raw.get("geminiModel") or os.environ.get("GEMINI_MODEL") or "gemini-4-hydrogen-medium"),
            codex_model=str(raw.get("codexModel") or os.environ.get("CODEX_MODEL") or "gpt-6.1-sol"),
            flaky_test_runs=max(1, int(raw.get("flakyTestRuns") or 2)),
            max_benchmark_regression_pct=float(raw.get("maxBenchmarkRegressionPct") or 5.0),
            gh_user=raw.get("ghUser") or os.environ.get("NIGHTWATCH_GH_USER") or None,
            git_email=raw.get("gitEmail") or os.environ.get("NIGHTWATCH_GIT_EMAIL") or None,
            branch_prefix=str(raw.get("branchPrefix") or os.environ.get("BRANCH_PREFIX") or "nightwatch/deps"),
            enable_os_notifications=_env_bool(raw.get("enableOsNotifications"), True),
            slack_webhook_url=raw.get("slackWebhookUrl") or os.environ.get("SLACK_WEBHOOK_URL") or None,
            email_webhook_url=raw.get("emailWebhookUrl") or os.environ.get("EMAIL_WEBHOOK_URL") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["repos"] = {k: asdict(v) for k, v in self.repos.items()}
        data["hygiene"] = asdict(self.hygiene)
        return data


@dataclass
class ShardNightwatchState:
    shard: str
    repo_path: str
    worktree_path: str
    branch_name: str
    base_branch: str
    ecosystem: str = "generic"  # rust | python | node | generic
    advisories: list[dict[str, str]] = field(default_factory=list)
    outdated_packages: list[dict[str, str]] = field(default_factory=list)
    compiler_warnings_count: int = 0
    compiler_warnings_sample: list[str] = field(default_factory=list)
    flaky_tests: list[str] = field(default_factory=list)
    test_suite_passed: bool = True
    upgraded_files: list[str] = field(default_factory=list)
    upgrader_used: str = "none"
    upgrade_summary: str = ""
    head_sha_before: str = ""
    head_sha_after: str = ""
    baseline_bench_ms: float | None = None
    candidate_bench_ms: float | None = None
    benchmark_delta_pct: float | None = None
    benchmark_passed: bool = True
    benchmark_summary: str = "skipped"
    green_ready_to_merge: bool = False
    pr_url: str | None = None
    status: str = "initialized"  # initialized | audited | upgraded | benchmarked | reported

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ShardNightwatchState":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in known})


class NightwatchRunStore:
    """File-backed state store rooted in `/tmp/rippflow-nightwatch` on read-only cloud containers or `.runs` locally."""

    def __init__(self, workflow_dir: Path, request_id: str, *, rippflow_env: str | None = None) -> None:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", request_id)
        rt = (rippflow_env or detect_rippflow_env()).strip().lower()
        explicit_state_dir = os.environ.get("RIPPFLOW_STATE_DIR")
        if explicit_state_dir:
            base_runs = Path(explicit_state_dir)
        elif rt == "cloud" and not os.access(workflow_dir, os.W_OK):
            base_runs = Path("/tmp/rippflow-nightwatch/.runs")
        else:
            base_runs = workflow_dir / ".runs"

        self.workflow_dir = workflow_dir
        self.request_id = request_id
        self.run_dir = base_runs / safe_id
        self.shards_dir = self.run_dir / "shards"
        self.worktrees_dir = self.run_dir / "worktrees"

    def ensure_dirs(self) -> None:
        for d in (self.run_dir, self.shards_dir, self.worktrees_dir):
            d.mkdir(parents=True, exist_ok=True)

    def save_config(self, cfg: NightwatchConfig) -> None:
        self.ensure_dirs()
        (self.run_dir / "config.json").write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")

    def load_config(self) -> NightwatchConfig:
        raw = json.loads((self.run_dir / "config.json").read_text(encoding="utf-8"))
        repos = {k: NightwatchRepoSpec(**v) for k, v in raw.pop("repos", {}).items()}
        hygiene = HygieneOptions(**raw.pop("hygiene", {}))
        return NightwatchConfig(**raw, repos=repos, hygiene=hygiene)

    def save_hygiene_report(self, report: dict[str, Any]) -> None:
        self.ensure_dirs()
        (self.run_dir / "hygiene-reclaim.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def load_hygiene_report(self) -> dict[str, Any]:
        p = self.run_dir / "hygiene-reclaim.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    def shard_file(self, shard: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", shard)
        return self.shards_dir / f"{safe}.json"

    def save_shard(self, state: ShardNightwatchState) -> None:
        self.ensure_dirs()
        tmp = self.shard_file(state.shard).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.shard_file(state.shard))

    def load_shard(self, shard: str) -> ShardNightwatchState:
        return ShardNightwatchState.from_dict(json.loads(self.shard_file(shard).read_text(encoding="utf-8")))

    def load_all_shards(self) -> list[ShardNightwatchState]:
        if not self.shards_dir.exists():
            return []
        return [
            ShardNightwatchState.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(self.shards_dir.glob("*.json"))
        ]
