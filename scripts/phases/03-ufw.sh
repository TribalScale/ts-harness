#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="03-ufw"
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

echo "[phase:${PHASE_NAME}] Configuring UFW (allow 2222 before SSH hardening)..."

if ! ufw status | grep -q "Status: active"; then
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 2222/tcp comment "SSH (hardened)"
    ufw allow 22/tcp comment "SSH bootstrap — removed after sshd listens on 2222"
    ufw --force enable
    echo "[phase:${PHASE_NAME}] UFW enabled with 22/tcp (temporary) and 2222/tcp."
else
    ufw allow 2222/tcp comment "SSH" 2>/dev/null || true
    ufw allow 22/tcp comment "SSH bootstrap" 2>/dev/null || true
    echo "[phase:${PHASE_NAME}] UFW already active; ensured 22/tcp and 2222/tcp are allowed."
fi

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
