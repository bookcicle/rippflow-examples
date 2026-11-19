# Rippflow Chat Agent Plugins

This directory contains three working example chat-agent plugins demonstrating Rippflow's extensible plugin architecture across:
1. **CLI transport** (`plugins/ollama/`): Zero-code declarative YAML manifest invoking local CLI binaries directly with templated `{prompt}` and `{model}` placeholders.
2. **Python `stdio` transport** (`plugins/py-repo-analyst/`): Pure Python 3 standard library agent consuming `rippflow-agent/v1` JSON over `stdin` and emitting streaming NDJSON `thinking` and `delta` events.
3. **JavaScript/Node.js `stdio` transport** (`plugins/js-workspace-helper/`): Zero-dependency Node.js ES module consuming `rippflow-agent/v1` JSON, inspecting project manifests (`package.json`, `Cargo.toml`, `pyproject.toml`), and emitting structured markdown tables and error events.

---

## Plugin Overview

| Plugin ID | Transport | Binary | Manifest Format | Language | Description |
|---|---|---|---|---|---|
| `ollama` | `cli` | `ollama` | YAML (`ollama.yaml`) | Declarative | Zero-code local LLM runner supporting models like `llama3.2`, `qwen2.5-coder`, `deepseek-r1`, `mistral`. |
| `py-repo-analyst` | `stdio` | `python3` | YAML (`py-repo-analyst.yaml`) | Python 3 stdlib | Inspects `cwd` (git branch, working tree changes, recent commits, file counts, `rippflow.yaml` configuration) and answers questions. Queries local Ollama if reachable, falling back cleanly to deterministic analysis. |
| `js-workspace-helper` | `stdio` | `node` | JSON (`js-workspace-helper.json`) | Node.js ESM | Analyzes `package.json`, `Cargo.toml`, and `pyproject.toml` manifests in `cwd`, emitting tables of scripts, build settings, and dependencies. |
| `wasm-sec-audit` | `wasm` | *(embedded WASI)* | YAML (`wasm-sec-audit.yaml`) | Rust → `wasm32-wasip1` | Air-gapped security & dependency auditor running in a read-only WASI sandbox (`read: ["."]`, `write: []`, no network) with a static UI rules tab (`static_overview.json`). |
| `wasm-trace-lens` | `wasm` | *(embedded WASI)* | YAML (`wasm-trace-lens.yaml`) | Rust → `wasm32-wasip1` | Dynamic WASI UI provider: scans the active workspace in-process (`action: "view"`) and emits live Declarative UI JSON (`MetricCard`, `BarChart`, `VirtualizedTable`) with no background daemon. |
| `ds-evals` | `cli` | `python3` | YAML (`ds-evals.yaml`) | Declarative UI | Data science model evaluation studio: live loss curves (`LineChart`), confusion matrix (`ConfusionMatrix`), and checkpoints drawer (`tree`). |
| `pipeline-dag` | `cli` | `python3` | YAML (`pipeline-dag.yaml`) | Declarative UI | Pipeline and workflow DAG visualizer: interactive node graph (`DagFlow`) and pipeline history drawer. |
| `cluster-pulse` | `cli` | `python3` | YAML (`cluster-pulse.yaml`) | Declarative UI + Icons | Real-time GPU & compute telemetry tab, compute nodes drawer, and custom SVG hardware icon pack (`icons/`). |
| `neon-icons` | `cli` | — | YAML (`neon-icons.yaml`) | Icon Pack (`ui`) | Studio-wide SVG icon pack overriding 52 toolbar, file tree, navigation, and window control icons. |
| `lucide-icons` | `cli` | — | YAML (`lucide-icons.yaml`) | Icon Pack (`ui`) | Open-source Lucide icon pack (ISC License) covering 68 Studio UI, file, navigation, and hardware icons. |
| `fontawesome-icons` | `cli` | — | YAML (`fontawesome-icons.yaml`) | Icon Pack (`ui`) | Open-source Font Awesome Free 6.x icon pack (CC BY 4.0 / MIT) covering 76 Studio UI and brand icons. |
| `solar-icons` | `cli` | — | YAML (`solar-icons.yaml`) | Icon Pack (`ui`) | Bubbly, rounded Solar Bold Duotone icon pack (CC BY 4.0) covering 91 Studio UI, file, navigation, and brand icons. |
| `fluent-emoji-icons` | `cli` | — | YAML (`fluent-emoji-icons.yaml`) | Color Icon Pack (`ui`) | Playful, bubbly full-color Microsoft Fluent Emoji Flat pack (MIT, `multicolor: true`) covering 91 Studio UI, file, and brand icons. |
| `local-ports` | `cli` | `python3` | YAML (`local-ports.yaml`) | Drawer + Dynamic UI | Sidebar drawer listing active TCP ports & Docker containers; clicking any item opens a live Declarative UI tab. |

---

## The `rippflow-agent/v1` Protocol

Plugins using `stdio` transport communicate with Rippflow Studio via standard input and standard output.

### 1. Request (`stdin`)
Rippflow sends a single JSON line to the plugin's standard input:
```json
{
  "protocol": "rippflow-agent/v1",
  "action": "chat",
  "agent": "py-repo-analyst",
  "prompt": "Summarize the uncommitted changes in this repository",
  "model": "llama3.2",
  "cwd": "/path/to/workspace"
}
```

### 2. Events (`stdout`)
The plugin responds by emitting NDJSON lines (newline-delimited JSON) to `stdout`:

- **Thinking / Working trace**:
  ```json
  {"type": "thinking", "text": "Inspecting git branch and working tree changes..."}
  ```
- **Markdown output delta**:
  ```json
  {"type": "delta", "text": "## Repository Analysis: `rippflow-examples`\n..."}
  ```
- **Error event**:
  ```json
  {"type": "error", "text": "Target workspace directory does not exist: /invalid/path"}
  ```

---

## Declarative UI Plugins (Tabs, Drawers, and Icon Packs)

Rippflow plugins can contribute rich interactive Studio tabs, custom sidebar drawers, and custom SVG icon packs through the `contributes` manifest section:

```bash
# 1. Install Data Science Model Training & Evals Studio
rippflow plugin add plugins/ds-evals/ds-evals.yaml

# 2. Install Pipeline DAG Visualizer
rippflow plugin add plugins/pipeline-dag/pipeline-dag.yaml

# 3. Install Cluster & Hardware Telemetry (with custom hardware icon pack)
rippflow plugin add plugins/cluster-pulse/cluster-pulse.yaml
```

Each plugin includes a runnable simulation script for instant validation:
```bash
# Stream training loss and generate model checkpoints
python3 plugins/ds-evals/mock_train.py

# Simulate multi-step pipeline DAG state transitions
python3 plugins/pipeline-dag/mock_pipeline.py

# Stream GPU and VRAM hardware metrics
python3 plugins/cluster-pulse/pulse_collector.py
```

---

## Installation

Run `install.sh` to install all three plugins into `~/.rippflow/plugins/` (and mirror them into `~/.rippflow/agents/`):

```bash
bash plugins/install.sh
```

### How `install.sh` Works
1. Copies `plugins/ollama/ollama.yaml` directly into `~/.rippflow/plugins/ollama.yaml`.
2. Copies `agent.py` to `~/.rippflow/plugins/py-repo-analyst/agent.py` and `agent.js` to `~/.rippflow/plugins/js-workspace-helper/agent.js`.
3. Makes both agent scripts executable (`chmod +x`).
4. Rewrites the relative script paths in `py-repo-analyst.yaml` and `js-workspace-helper.json` into absolute paths so that Rippflow Studio can launch them from any working directory.
5. Mirrors manifests to `~/.rippflow/agents/` for seamless auto-discovery across both legacy and new plugin paths.

You can also pass a custom destination directory:
```bash
bash plugins/install.sh /custom/plugin/dir
export RIPPFLOW_AGENT_PLUGINS_DIR="/custom/plugin/dir"
```

---

## Testing in Rippflow Studio

Once installed, test the plugins in Rippflow Studio:

1. **Start Rippflow Studio**:
   ```bash
   rippflow dev
   ```
2. **Check Doctor Status**:
   - Open the **Doctor** tab in Rippflow Studio.
   - The Doctor tab validates PATH availability for `ollama`, `python3`, and `node`, showing display names, install commands, and status details.
3. **Use Chat Mode (`/ask`)**:
   - In the Studio command bar, switch to Chat Mode by typing `/ask`.
   - Select your preferred agent:
     - `@ollama` to run prompts against local Ollama models (`llama3.2`, `mistral`, etc.).
     - `@py-repo-analyst` to get repository status, git summaries, and file metrics.
     - `@js-workspace-helper` to inspect `package.json`, `Cargo.toml`, or `pyproject.toml` manifests.

---

## Running Automated Tests

Run the Python test suite to validate all manifests and test both stdio agents end-to-end:

```bash
python3 -m unittest discover -s plugins -p "test_*.py" -v
```
