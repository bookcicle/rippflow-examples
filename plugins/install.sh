#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Rippflow Agent Plugins Installer
# Copies example plugins into ~/.rippflow/plugins/ (and ~/.rippflow/agents/)
# rewriting relative script paths into absolute paths safely.
# -----------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
TARGET_INPUT="${1:-${RIPPFLOW_AGENT_PLUGINS_DIR:-$HOME/.rippflow/plugins}}"

# Canonicalize destination directory to guarantee absolute paths
mkdir -p "${TARGET_INPUT}"
TARGET_DIR="$(cd "${TARGET_INPUT}" && pwd -P)"

# Canonicalize agents mirror directory
AGENTS_INPUT="${HOME:-/tmp}/.rippflow/agents"
mkdir -p "${AGENTS_INPUT}"
AGENTS_DIR="$(cd "${AGENTS_INPUT}" && pwd -P)"

echo "=== Installing Rippflow Example Agent Plugins ==="
echo "Source:      ${SCRIPT_DIR}"
echo "Destination: ${TARGET_DIR}"

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
python3 -c "
import sys
src_manifest, dst_manifest, script_abs = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src_manifest, 'r', encoding='utf-8') as f:
    lines = f.read().splitlines()
new_lines = []
for line in lines:
    trimmed = line.strip()
    if trimmed.startswith('- ') and ('agent.py' in trimmed):
        indent = line[:line.find('-')]
        escaped = script_abs.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\"')
        new_lines.append(f'{indent}- \"{escaped}\"')
    else:
        new_lines.append(line)
with open(dst_manifest, 'w', encoding='utf-8') as f:
    f.write('\n'.join(new_lines) + '\n')
" "${SCRIPT_DIR}/py-repo-analyst/py-repo-analyst.yaml" \
  "${TARGET_DIR}/py-repo-analyst.yaml" \
  "${PY_SCRIPT_ABS}"

# 3. Install JavaScript Workspace Helper (Node.js stdio transport)
echo "-> Installing 'js-workspace-helper' plugin..."
cp "${SCRIPT_DIR}/js-workspace-helper/agent.mjs" "${TARGET_DIR}/js-workspace-helper/agent.mjs"
chmod +x "${TARGET_DIR}/js-workspace-helper/agent.mjs"

JS_SCRIPT_ABS="${TARGET_DIR}/js-workspace-helper/agent.mjs"
python3 -c "
import json, sys
src_manifest, dst_manifest, script_abs = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src_manifest, 'r', encoding='utf-8') as f:
    spec = json.load(f)
spec['invoke']['args'] = [script_abs]
with open(dst_manifest, 'w', encoding='utf-8') as f:
    json.dump(spec, f, indent=2)
    f.write('\n')
" "${SCRIPT_DIR}/js-workspace-helper/js-workspace-helper.json" \
  "${TARGET_DIR}/js-workspace-helper.json" \
  "${JS_SCRIPT_ABS}"

# 4. Mirror manifests into ~/.rippflow/agents/ for built-in discovery
if [[ "${TARGET_DIR}" != "${AGENTS_DIR}" ]] && [[ -n "${HOME:-}" ]] && [[ -d "${HOME}" ]]; then
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
