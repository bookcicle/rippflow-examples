#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Rippflow Agent Plugins Installer
# Copies example plugins into ~/.rippflow/plugins/ (and ~/.rippflow/agents/)
# rewriting relative script paths into absolute paths.
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-${RIPPFLOW_AGENT_PLUGINS_DIR:-$HOME/.rippflow/plugins}}"
AGENTS_DIR="${HOME:-/tmp}/.rippflow/agents"

echo "=== Installing Rippflow Example Agent Plugins ==="
echo "Source:      ${SCRIPT_DIR}"
echo "Destination: ${TARGET_DIR}"

mkdir -p "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}/py-repo-analyst"
mkdir -p "${TARGET_DIR}/js-workspace-helper"

# 1. Install Ollama (CLI transport, zero-code manifest)
echo "-> Installing 'ollama' plugin manifest..."
cp "${SCRIPT_DIR}/ollama/ollama.yaml" "${TARGET_DIR}/ollama.yaml"

# 2. Install Python Repo Analyst (Python stdio transport)
echo "-> Installing 'py-repo-analyst' plugin..."
cp "${SCRIPT_DIR}/py-repo-analyst/agent.py" "${TARGET_DIR}/py-repo-analyst/agent.py"
chmod +x "${TARGET_DIR}/py-repo-analyst/agent.py"

PY_SCRIPT_ABS="${TARGET_DIR}/py-repo-analyst/agent.py"
# Rewrite relative script path to absolute path in the destination manifest
sed "s|agent\.py|${PY_SCRIPT_ABS}|g" \
  "${SCRIPT_DIR}/py-repo-analyst/py-repo-analyst.yaml" > "${TARGET_DIR}/py-repo-analyst.yaml"

# 3. Install JavaScript Workspace Helper (Node.js stdio transport)
echo "-> Installing 'js-workspace-helper' plugin..."
cp "${SCRIPT_DIR}/js-workspace-helper/agent.mjs" "${TARGET_DIR}/js-workspace-helper/agent.mjs"
chmod +x "${TARGET_DIR}/js-workspace-helper/agent.mjs"

JS_SCRIPT_ABS="${TARGET_DIR}/js-workspace-helper/agent.mjs"
# Rewrite relative script path to absolute path in the destination manifest
sed "s|\"agent\.mjs\"|\"${JS_SCRIPT_ABS}\"|g" \
  "${SCRIPT_DIR}/js-workspace-helper/js-workspace-helper.json" > "${TARGET_DIR}/js-workspace-helper.json"

# 4. Mirror manifests into ~/.rippflow/agents/ for built-in discovery
if [[ "${TARGET_DIR}" != "${AGENTS_DIR}" ]] && [[ -d "${HOME:-}" ]]; then
  echo "-> Mirroring manifests into ${AGENTS_DIR}..."
  mkdir -p "${AGENTS_DIR}"
  cp "${TARGET_DIR}/ollama.yaml" "${AGENTS_DIR}/ollama.yaml"
  cp "${TARGET_DIR}/py-repo-analyst.yaml" "${AGENTS_DIR}/py-repo-analyst.yaml"
  cp "${TARGET_DIR}/js-workspace-helper.json" "${AGENTS_DIR}/js-workspace-helper.json"
fi

echo ""
echo "✓ Successfully installed plugins:"
echo "  1. ollama              -> ${TARGET_DIR}/ollama.yaml"
echo "  2. py-repo-analyst     -> ${TARGET_DIR}/py-repo-analyst.yaml"
echo "  3. js-workspace-helper -> ${TARGET_DIR}/js-workspace-helper.json"
echo ""
echo "To use in Rippflow Studio:"
echo "  - Ensure RIPPFLOW_AGENT_PLUGINS_DIR=\"${TARGET_DIR}\" (or use default ~/.rippflow/agents)"
echo "  - Launch Rippflow Studio ('rippflow dev' or 'rippflow warp-shell')"
echo "  - Open Doctor tab to verify plugin health"
echo "  - Enter Chat Mode (/ask) and switch between Ollama, py-repo-analyst, and js-workspace-helper"
