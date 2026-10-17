#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="11f-check-embedding-dim"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"

if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        echo "[phase:${PHASE_NAME}] Already complete for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

echo "[phase:${PHASE_NAME}] Running embedding dimension guardrail..."

CHECK_SCRIPT="$SCRIPT_DIR/check-memory-embedding-dimension.sh"
if [[ ! -f "$CHECK_SCRIPT" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $CHECK_SCRIPT not found" >&2
    exit 1
fi
chmod +x "$CHECK_SCRIPT"

sudo -u "$ADMIN_USER" \
    env TS_HARNESS_CONFIG_DIR="$TS_HARNESS_CONFIG_DIR" \
        TS_HARNESS_WORKSPACE_DIR="${TS_HARNESS_WORKSPACE_DIR:-${TS_HARNESS_CONFIG_DIR}/workspace}" \
        ENTERPRISE_MEMORY_EMBEDDING_DIM="${ENTERPRISE_MEMORY_EMBEDDING_DIM:-1536}" \
        ENTERPRISE_EMBEDDING_CHECK_STRICT="${ENTERPRISE_EMBEDDING_CHECK_STRICT:-0}" \
    "$CHECK_SCRIPT"

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
