#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"


PHASE_NAME="07-workspace-dirs"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"


if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        write_instance_env
        echo "[phase:${PHASE_NAME}] Already complete for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

echo "[phase:${PHASE_NAME}] Creating workspace directories..."
mkdir -p "$TS_HARNESS_CONFIG_DIR" "$TS_HARNESS_WORKSPACE_DIR"
chown -R "$ADMIN_USER:$ADMIN_USER" "$DATA_DIR"
echo "[phase:${PHASE_NAME}] Created $TS_HARNESS_CONFIG_DIR and $TS_HARNESS_WORKSPACE_DIR"

echo "[phase:${PHASE_NAME}] Configuring git credentials..."
GITHUB_TOKEN="$(fetch_secret "github-token" true)"
if is_blank_or_placeholder "$GITHUB_TOKEN"; then
    echo "[phase:${PHASE_NAME}] WARNING: No github-token in Secrets Manager — git clones only work for public repos."
else
    GIT_CRED_FILE="$ADMIN_HOME/.git-credentials"
    echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > "$GIT_CRED_FILE"
    chown "$ADMIN_USER:$ADMIN_USER" "$GIT_CRED_FILE"
    chmod 600 "$GIT_CRED_FILE"
    sudo -u "$ADMIN_USER" git config --global credential.helper "store --file=$GIT_CRED_FILE"
    echo "[phase:${PHASE_NAME}] Git credential helper configured for $ADMIN_USER."
fi

mkdir -p "$DATA_DIR/.setup"
write_instance_env
echo "[phase:${PHASE_NAME}] Wrote bootstrap env at $(instance_env_path)"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
