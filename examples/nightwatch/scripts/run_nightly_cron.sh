#!/usr/bin/env bash
# Nightly OS cron trigger for `rippflow-nightwatch` (`0 2 * * *` -> 2:00 AM local time).
# Ensures the persistent local HTTPS server on `:8445` is running, reads its current
# bearer token dynamically from `.outputs/`, and triggers the nightly run.

set -euo pipefail

export PATH="/usr/local/bin:${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

WORKFLOW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${WORKFLOW_DIR}"

PORT="${NIGHTWATCH_PORT:-8445}"
SHARDS="${NIGHTWATCH_SHARDS:-rippflow-core,rippflow-cli,rippflow-starter,rippflow-docs}"
PAYLOAD_FILE="${WORKFLOW_DIR}/nightwatch.local.json"
if [[ ! -f "${PAYLOAD_FILE}" ]]; then
  PAYLOAD_FILE="${WORKFLOW_DIR}/examples/nightwatch_input.example.json"
fi

mkdir -p "${WORKFLOW_DIR}/.outputs"
CRON_LOG="${WORKFLOW_DIR}/.outputs/nightly-cron.log"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting rippflow-nightwatch cron trigger (port=${PORT})" >> "${CRON_LOG}"

# Ensure persistent local HTTPS server is alive on :8445 (auto-restarts after reboot/stop)
if ! curl -sk --max-time 3 "https://localhost:${PORT}/v1/status" >/dev/null 2>&1; then
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Port ${PORT} not responding; starting rippflow deploy --local..." >> "${CRON_LOG}"
  rippflow deploy --local workflow.yaml \
    --port "${PORT}" \
    --rippflow-env local \
    --shards "${SHARDS}" >> "${CRON_LOG}" 2>&1
  sleep 2
fi

# Extract bearer token dynamically from `rippflow deploy --local --status`
TOKEN="$(rippflow deploy --local --status | grep -A2 "localhost:${PORT}/" | sed -n 's/.*Bearer \(rf_tok_[A-Za-z0-9_-]*\).*/\1/p' | head -n 1)"

if [[ -z "${TOKEN}" ]]; then
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] ERROR: Could not locate bearer token for port ${PORT}; falling back to direct rippflow run" >> "${CRON_LOG}"
  rippflow run workflow.yaml \
    --handlers handlers.json \
    --rippflow-env local \
    --shards "${SHARDS}" \
    --input "${PAYLOAD_FILE}" \
    --timeout-seconds 7200 >> "${CRON_LOG}" 2>&1
  exit 0
fi

HTTP_CODE="$(curl -sk -o "${WORKFLOW_DIR}/.outputs/last-cron-response.json" -w "%{http_code}" \
  -X POST "https://localhost:${PORT}/v1/workflows/rippflow-nightwatch/run" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary "@${PAYLOAD_FILE}")"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Triggered rippflow-nightwatch via https://localhost:${PORT} -> HTTP ${HTTP_CODE}" >> "${CRON_LOG}"
