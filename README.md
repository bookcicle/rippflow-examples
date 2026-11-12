# Rippflow Examples

Open-source, local-first [Rippflow](https://rippflow.com) workflows showcasing sharded DAG orchestration, fast stateless handler slices (`python-light` / `python-standard`), durable multi-hour `async_tool` HTTPS HMAC callbacks (`workerCapacityReleased: true`), bounded rework loops, and human-in-the-loop gates.

---

## Included Examples

| Example | Path | Description |
| :--- | :--- | :--- |
| **Multi-Repo AI Feature & Dual-Identity PR Review Pipeline** | [`examples/pr-pipeline`](./examples/pr-pipeline) | Accepts a natural-language feature request + target repositories, authors PRs in isolated git worktrees under a dedicated bot GitHub identity, runs local test suites, conducts incremental `codex exec resume` code review under a separate reviewer identity, loops up to 15 turns, and escalates unresolved shards via macOS/Linux/Windows desktop notifications + authenticated Rippflow callbacks. |

---

## Quickstart (`examples/pr-pipeline`)

```bash
cd examples/pr-pipeline

# 1. Create virtualenv & install dependencies
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# 2. Configure your local workspace & GitHub identities (gitignored)
cp .env.example .env
# Edit .env to set WORKSPACE_DIR, AUTHOR_GH_USER, AUTHOR_GIT_EMAIL, REVIEWER_GH_USER

# 3. Validate workflow & start persistent local HTTPS ingress + callback server
rippflow validate workflow.yaml --handlers handlers.json
rippflow deploy --local workflow.yaml --shards service-core,service-cli

# 4. Open Rippflow Studio with live 1s Analytics polling & clickable server logs
rippflow studio --command "rippflow visualize .outputs --split"
```

---

## License

MIT — see [LICENSE](./LICENSE).
