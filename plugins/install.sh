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
cp "${SCRIPT_DIR}/js-workspace-helper/agent.js" "${TARGET_DIR}/js-workspace-helper/agent.js"
chmod +x "${TARGET_DIR}/js-workspace-helper/agent.js"

JS_SCRIPT_ABS="${TARGET_DIR}/js-workspace-helper/agent.js"
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

# 4. Install Wasm Security & Dependency Auditor (WASI transport + Static UI Tab)
if [[ -f "${SCRIPT_DIR}/wasm-sec-audit/wasm-sec-audit.wasm" ]]; then
  echo "-> Installing 'wasm-sec-audit' plugin..."
  mkdir -p "${TARGET_DIR}/wasm-sec-audit"
  cp "${SCRIPT_DIR}/wasm-sec-audit/wasm-sec-audit.wasm" "${TARGET_DIR}/wasm-sec-audit/wasm-sec-audit.wasm"
  cp "${SCRIPT_DIR}/wasm-sec-audit/static_overview.json" "${TARGET_DIR}/wasm-sec-audit/static_overview.json"
  WASM_MOD_ABS="${TARGET_DIR}/wasm-sec-audit/wasm-sec-audit.wasm"
  WASM_VIEW_ABS="${TARGET_DIR}/wasm-sec-audit/static_overview.json"
  sed -e "s|module: wasm-sec-audit.wasm|module: \"${WASM_MOD_ABS}\"|" \
      -e "s|view: static_overview.json|view: \"${WASM_VIEW_ABS}\"|" \
    "${SCRIPT_DIR}/wasm-sec-audit/wasm-sec-audit.yaml" > "${TARGET_DIR}/wasm-sec-audit.yaml"
fi

# 5. Install Wasm Live Workspace Telemetry (Dynamic WASI UI Tab)
if [[ -f "${SCRIPT_DIR}/wasm-trace-lens/wasm-trace-lens.wasm" ]]; then
  echo "-> Installing 'wasm-trace-lens' plugin..."
  mkdir -p "${TARGET_DIR}/wasm-trace-lens"
  cp "${SCRIPT_DIR}/wasm-trace-lens/wasm-trace-lens.wasm" "${TARGET_DIR}/wasm-trace-lens/wasm-trace-lens.wasm"
  WASM_TRACE_ABS="${TARGET_DIR}/wasm-trace-lens/wasm-trace-lens.wasm"
  sed "s|module: wasm-trace-lens.wasm|module: \"${WASM_TRACE_ABS}\"|" \
    "${SCRIPT_DIR}/wasm-trace-lens/wasm-trace-lens.yaml" > "${TARGET_DIR}/wasm-trace-lens.yaml"
fi

# 5b. Install Neon Studio Icons (Icon Pack Plugin)
if [[ -d "${SCRIPT_DIR}/neon-icons/icons" ]]; then
  echo "-> Installing 'neon-icons' icon pack plugin..."
  mkdir -p "${TARGET_DIR}/neon-icons/icons"
  cp "${SCRIPT_DIR}/neon-icons/icons/"*.svg "${TARGET_DIR}/neon-icons/icons/"
  NEON_ICONS_ABS="${TARGET_DIR}/neon-icons/icons"
  sed "s|path: icons/|path: \"${NEON_ICONS_ABS}\"|" \
    "${SCRIPT_DIR}/neon-icons/neon-icons.yaml" > "${TARGET_DIR}/neon-icons.yaml"
fi

# 5c. Install Lucide OSS Icon Pack Plugin
if [[ -d "${SCRIPT_DIR}/lucide-icons/icons" ]]; then
  echo "-> Installing 'lucide-icons' icon pack plugin..."
  mkdir -p "${TARGET_DIR}/lucide-icons/icons"
  cp "${SCRIPT_DIR}/lucide-icons/icons/"*.svg "${TARGET_DIR}/lucide-icons/icons/"
  cp "${SCRIPT_DIR}/lucide-icons/LICENSE" "${TARGET_DIR}/lucide-icons/LICENSE"
  LUCIDE_ICONS_ABS="${TARGET_DIR}/lucide-icons/icons"
  sed "s|path: icons/|path: \"${LUCIDE_ICONS_ABS}\"|" \
    "${SCRIPT_DIR}/lucide-icons/lucide-icons.yaml" > "${TARGET_DIR}/lucide-icons.yaml"
fi

# 5d. Install Font Awesome Free OSS Icon Pack Plugin
if [[ -d "${SCRIPT_DIR}/fontawesome-icons/icons" ]]; then
  echo "-> Installing 'fontawesome-icons' icon pack plugin..."
  mkdir -p "${TARGET_DIR}/fontawesome-icons/icons"
  cp "${SCRIPT_DIR}/fontawesome-icons/icons/"*.svg "${TARGET_DIR}/fontawesome-icons/icons/"
  cp "${SCRIPT_DIR}/fontawesome-icons/LICENSE.txt" "${TARGET_DIR}/fontawesome-icons/LICENSE.txt"
  FA_ICONS_ABS="${TARGET_DIR}/fontawesome-icons/icons"
  sed "s|path: icons/|path: \"${FA_ICONS_ABS}\"|" \
    "${SCRIPT_DIR}/fontawesome-icons/fontawesome-icons.yaml" > "${TARGET_DIR}/fontawesome-icons.yaml"
fi

# 5e. Install Local Ports & Containers Drawer + Dynamic Tabs Plugin
if [[ -f "${SCRIPT_DIR}/local-ports/local_ports.py" ]]; then
  echo "-> Installing 'local-ports' drawer plugin..."
  mkdir -p "${TARGET_DIR}/local-ports"
  cp "${SCRIPT_DIR}/local-ports/local_ports.py" "${TARGET_DIR}/local-ports/local_ports.py"
  chmod +x "${TARGET_DIR}/local-ports/local_ports.py"
  LOCAL_PORTS_ABS="${TARGET_DIR}/local-ports/local_ports.py"
  sed "s|local_ports.py|${LOCAL_PORTS_ABS}|g" \
    "${SCRIPT_DIR}/local-ports/local-ports.yaml" > "${TARGET_DIR}/local-ports.yaml"
fi

# 6. Mirror manifests into ~/.rippflow/agents/ for built-in discovery
if [[ "${TARGET_DIR}" != "${AGENTS_DIR}" ]] && [[ -n "${HOME:-}" ]] && [[ -d "${HOME}" ]]; then
  echo "-> Mirroring manifests into ${AGENTS_DIR}..."
  mkdir -p "${AGENTS_DIR}"
  cp "${TARGET_DIR}/ollama.yaml" "${AGENTS_DIR}/ollama.yaml"
  cp "${TARGET_DIR}/py-repo-analyst.yaml" "${AGENTS_DIR}/py-repo-analyst.yaml"
  cp "${TARGET_DIR}/js-workspace-helper.json" "${AGENTS_DIR}/js-workspace-helper.json"
  if [[ -f "${TARGET_DIR}/wasm-sec-audit.yaml" ]]; then
    cp "${TARGET_DIR}/wasm-sec-audit.yaml" "${AGENTS_DIR}/wasm-sec-audit.yaml"
  fi
  if [[ -f "${TARGET_DIR}/wasm-trace-lens.yaml" ]]; then
    cp "${TARGET_DIR}/wasm-trace-lens.yaml" "${AGENTS_DIR}/wasm-trace-lens.yaml"
  fi
  if [[ -f "${TARGET_DIR}/neon-icons.yaml" ]]; then
    cp "${TARGET_DIR}/neon-icons.yaml" "${AGENTS_DIR}/neon-icons.yaml"
  fi
  if [[ -f "${TARGET_DIR}/lucide-icons.yaml" ]]; then
    cp "${TARGET_DIR}/lucide-icons.yaml" "${AGENTS_DIR}/lucide-icons.yaml"
  fi
  if [[ -f "${TARGET_DIR}/fontawesome-icons.yaml" ]]; then
    cp "${TARGET_DIR}/fontawesome-icons.yaml" "${AGENTS_DIR}/fontawesome-icons.yaml"
  fi
  if [[ -f "${TARGET_DIR}/local-ports.yaml" ]]; then
    cp "${TARGET_DIR}/local-ports.yaml" "${AGENTS_DIR}/local-ports.yaml"
  fi
fi

echo ""
echo "✓ Successfully installed plugins:"
echo "  1. ollama              -> ${TARGET_DIR}/ollama.yaml"
echo "  2. py-repo-analyst     -> ${TARGET_DIR}/py-repo-analyst.yaml"
echo "  3. js-workspace-helper -> ${TARGET_DIR}/js-workspace-helper.json"
if [[ -f "${TARGET_DIR}/wasm-sec-audit.yaml" ]]; then
  echo "  4. wasm-sec-audit      -> ${TARGET_DIR}/wasm-sec-audit.yaml"
fi
if [[ -f "${TARGET_DIR}/wasm-trace-lens.yaml" ]]; then
  echo "  5. wasm-trace-lens     -> ${TARGET_DIR}/wasm-trace-lens.yaml"
fi
if [[ -f "${TARGET_DIR}/neon-icons.yaml" ]]; then
  echo "  6. neon-icons          -> ${TARGET_DIR}/neon-icons.yaml"
fi
if [[ -f "${TARGET_DIR}/lucide-icons.yaml" ]]; then
  echo "  7. lucide-icons        -> ${TARGET_DIR}/lucide-icons.yaml"
fi
if [[ -f "${TARGET_DIR}/fontawesome-icons.yaml" ]]; then
  echo "  8. fontawesome-icons   -> ${TARGET_DIR}/fontawesome-icons.yaml"
fi
echo ""
echo "To use in Rippflow Studio:"
echo "  - Ensure RIPPFLOW_AGENT_PLUGINS_DIR=\"${TARGET_DIR}\" (or use default ~/.rippflow/agents)"
echo "  - Launch Rippflow Studio ('rippflow dev' or 'rippflow warp-shell')"
echo "  - Open Doctor tab to verify plugin health"
echo "  - Enter Chat Mode (/ask) and switch between Ollama, py-repo-analyst, and js-workspace-helper"
