# Rippflow Examples

> [!TIP]
> **Use this with Gemini Antigravity, Codex, or Claude Code!**
> 
> Rippflow natively supports **agent skills** and **local MCP** (Model Context Protocol). Connect your AI coding assistant with a single command to scaffold, validate, run, and inspect workflows directly from your agent chat:
> 
> ```bash
> # Automatically configure agent skills and local MCP across all installed clients
> rippflow mcp setup --client all
> ```
> *Supports Google Gemini Antigravity, OpenAI Codex, and Anthropic Claude Code.*

Open-source, local- and cloud-ready [Rippflow](https://rippflow.com) workflows showcasing sharded DAG orchestration, fast stateless handler slices (`python-light` / `python-standard`), environment-aware execution (`--rippflow-env local` vs `--rippflow-env cloud`), durable multi-hour `async_tool` callbacks (`workerCapacityReleased: true`), bounded rework loops, and human-in-the-loop gates.

---

## Included Examples

Explore all runnable workflow examples in [`examples/`](https://github.com/bookcicle/rippflow-examples/tree/main/examples):

| Example | Path | Runtime Tiers | Description |
| :--- | :--- | :--- | :--- |
| **Multi-Repo AI Feature & Dual-Identity PR Review Pipeline** | [`examples/pr-pipeline`](./examples/pr-pipeline) | `local` | Accepts a natural-language feature request + target repositories, authors PRs in isolated git worktrees under a dedicated bot GitHub identity, runs local test suites, conducts incremental `codex exec resume` code review under a separate reviewer identity, loops up to 15 turns, and escalates unresolved shards via macOS/Linux/Windows desktop notifications + authenticated Rippflow callbacks. |
| **Multi-Repo Release & Sync Train** | [`examples/release-train`](./examples/release-train) | `local` & `cloud` | Coordinates multi-repo drift detection, version & pinned-SHA bumps (`PINNED_SOURCE_SHAS.json`), changelog drafting (Gemini/Codex in `local`, automatically flagged off to zero-dependency deterministic Conventional Commits in `cloud`), parallel build + SHA-256 checksum & benchmark verification, a `24h` human release sign-off gate (OS banner + Slack/email webhooks), and ordered tag/publish + `RELEASE_NOTES.md` sync. |
| **Scheduled Overnight Repo Hygiene & Dependency Upgrader (`rippflow-nightwatch`)** | [`examples/nightwatch`](./examples/nightwatch) | `local` & `cloud` | Triggered nightly via cron or `/schedule`: reclaims disk space (prunes stale git worktrees, cleans Rust Cargo `target/` dirs, prunes unused Docker images/builders), audits advisories/outdated packages/warnings/flaky tests across shards, upgrades dependencies in isolated worktrees (optional Gemini/Codex breaking-API fixer in `local`, deterministic in `cloud`), gates on benchmark regressions, and delivers a morning OS notification + Slack/email digest with ready-to-merge green PR links. |

---

## Quickstart

### 1. Multi-Repo Release & Sync Train (`local` + `cloud`, Zero AI Dependencies Required)

```bash
cd examples/release-train

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# Validate both local and cloud handler profiles
rippflow validate workflow.yaml --handlers handlers.json --rippflow-env local
rippflow validate workflow.yaml --handlers handlers.json --rippflow-env cloud

# Run with cloud-tier rules (Codex/Gemini strictly flagged off)
rippflow run workflow.yaml --handlers handlers.json --rippflow-env cloud \
  --shards service-core,service-starter,service-docs \
  --input examples/release_input.example.json
```

### 2. Multi-Repo AI Feature & PR Review Pipeline (`examples/pr-pipeline`)

```bash
cd examples/pr-pipeline

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

cp .env.example .env
# Edit .env to set WORKSPACE_DIR, AUTHOR_GH_USER, AUTHOR_GIT_EMAIL, REVIEWER_GH_USER

rippflow validate workflow.yaml --handlers handlers.json
rippflow deploy --local workflow.yaml --shards service-core,service-cli
```

---

## License

MIT — see [LICENSE](./LICENSE).
