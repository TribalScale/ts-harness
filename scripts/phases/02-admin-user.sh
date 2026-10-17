#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="02-admin-user"
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

echo "[phase:${PHASE_NAME}] Configuring admin user '$ADMIN_USER'..."

if id "$ADMIN_USER" &>/dev/null; then
    echo "[phase:${PHASE_NAME}] User $ADMIN_USER already exists."
else
    adduser --disabled-password --gecos "TS Harness Admin" "$ADMIN_USER"
    usermod -aG sudo "$ADMIN_USER"
    echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$ADMIN_USER"
    chmod 0440 "/etc/sudoers.d/$ADMIN_USER"
    echo "[phase:${PHASE_NAME}] Created user $ADMIN_USER with passwordless sudo."
fi

mkdir -p "$ADMIN_HOME"
chown "$ADMIN_USER:$ADMIN_USER" "$ADMIN_HOME"

ADMIN_SSH_DIR="$ADMIN_HOME/.ssh"
mkdir -p "$ADMIN_SSH_DIR"

UBUNTU_AUTHKEYS="/home/ubuntu/.ssh/authorized_keys"
if [[ -f "$UBUNTU_AUTHKEYS" ]]; then
    cp "$UBUNTU_AUTHKEYS" "$ADMIN_SSH_DIR/authorized_keys"
    echo "[phase:${PHASE_NAME}] Copied SSH authorized_keys from ubuntu to $ADMIN_USER."
else
    echo "[phase:${PHASE_NAME}] WARNING: $UBUNTU_AUTHKEYS not found; admin user has no SSH keys."
fi

chown -R "$ADMIN_USER:$ADMIN_USER" "$ADMIN_SSH_DIR"
chmod 700 "$ADMIN_SSH_DIR"
chmod 600 "$ADMIN_SSH_DIR/authorized_keys" 2>/dev/null || true

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
