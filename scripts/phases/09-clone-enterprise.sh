#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="09-clone-enterprise"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"

if [[ -d "${ENTERPRISE_ASSETS_DIR:-}/.git" && -n "${ENTERPRISE_REPO_URL:-}" ]]; then
    if [[ -n "${ADMIN_USER:-}" ]] && id "$ADMIN_USER" &>/dev/null; then
        chown -R "$ADMIN_USER:$ADMIN_USER" "$ENTERPRISE_ASSETS_DIR"
    fi
    if [[ -n "${ADMIN_USER:-}" ]] && id "$ADMIN_USER" &>/dev/null; then
        sudo -u "$ADMIN_USER" git -C "$ENTERPRISE_ASSETS_DIR" remote set-url origin "$ENTERPRISE_REPO_URL"
    else
        git -C "$ENTERPRISE_ASSETS_DIR" remote set-url origin "$ENTERPRISE_REPO_URL"
    fi
fi

if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        echo "[phase:${PHASE_NAME}] Already complete for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

echo "[phase:${PHASE_NAME}] Syncing enterprise repo at $ENTERPRISE_ASSETS_DIR..."

if [[ -d "$ENTERPRISE_ASSETS_DIR/.git" ]]; then
    # Ensure proper ownership before git operations (may have been cloned as root in bootstrap).
    chown -R "$ADMIN_USER:$ADMIN_USER" "$ENTERPRISE_ASSETS_DIR"
    sudo -u "$ADMIN_USER" git -C "$ENTERPRISE_ASSETS_DIR" remote set-url origin "$ENTERPRISE_REPO_URL"
    sudo -u "$ADMIN_USER" git -C "$ENTERPRISE_ASSETS_DIR" pull --ff-only
    echo "[phase:${PHASE_NAME}] Enterprise assets updated."
else
    sudo -u "$ADMIN_USER" git clone "$ENTERPRISE_REPO_URL" "$ENTERPRISE_ASSETS_DIR"
    echo "[phase:${PHASE_NAME}] Enterprise assets cloned."
fi
sudo -u "$ADMIN_USER" git -C "$ENTERPRISE_ASSETS_DIR" remote set-url origin "$ENTERPRISE_REPO_URL"

if [[ ! -d "$SCRIPT_DIR" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: Enterprise repository must include a scripts/ directory (expected $SCRIPT_DIR)" >&2
    exit 1
fi
echo "[phase:${PHASE_NAME}] SCRIPT_DIR=$SCRIPT_DIR"

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
