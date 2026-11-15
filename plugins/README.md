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
