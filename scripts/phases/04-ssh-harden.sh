#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="04-ssh-harden"
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

echo "[phase:${PHASE_NAME}] Hardening SSH..."

SSHD_CONFIG="/etc/ssh/sshd_config"
cp "$SSHD_CONFIG" "${SSHD_CONFIG}.bak.$(date +%s)"

apply_sshd_option() {
    local key="$1" value="$2"
    if grep -qE "^[[:space:]]*#?[[:space:]]*${key}([[:space:]]|$)" "$SSHD_CONFIG"; then
        sed -i -E "s|^[[:space:]]*#?[[:space:]]*${key}([[:space:]].*)?$|${key} ${value}|" "$SSHD_CONFIG"
    else
        echo "${key} ${value}" >> "$SSHD_CONFIG"
    fi
}

apply_sshd_option "Port" "2222"
apply_sshd_option "PermitRootLogin" "no"
apply_sshd_option "PasswordAuthentication" "no"
apply_sshd_option "ChallengeResponseAuthentication" "no"
apply_sshd_option "KbdInteractiveAuthentication" "no"
apply_sshd_option "UsePAM" "yes"

mkdir -p /run/sshd
if ! sshd -t -f "$SSHD_CONFIG"; then
    echo "[phase:${PHASE_NAME}] ERROR: sshd_config validation failed — restoring backup" >&2
    LATEST_BACKUP="$(ls -t "${SSHD_CONFIG}.bak."* 2>/dev/null | head -1)"
    if [[ -n "$LATEST_BACKUP" ]]; then
        cp "$LATEST_BACKUP" "$SSHD_CONFIG"
    fi
    exit 1
fi

systemctl disable ssh.socket 2>/dev/null || true
systemctl stop ssh.socket 2>/dev/null || true
systemctl enable ssh.service 2>/dev/null || true
systemctl restart ssh
echo "[phase:${PHASE_NAME}] SSH hardened: port 2222, no root login, no password auth."

if ufw status | grep -q "Status: active"; then
    ufw delete allow 22/tcp 2>/dev/null || true
    echo "[phase:${PHASE_NAME}] Removed temporary UFW allow 22/tcp."
fi

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
