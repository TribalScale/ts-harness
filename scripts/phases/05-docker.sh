#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="05-docker"
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

# ---- Docker CE ----
echo "[phase:${PHASE_NAME}] Installing Docker CE..."
if command -v docker &>/dev/null; then
    echo "[phase:${PHASE_NAME}] Docker already installed: $(docker --version)"
else
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg lsb-release

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list

    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    systemctl enable docker
    systemctl start docker
    echo "[phase:${PHASE_NAME}] Docker installed: $(docker --version)"
fi

usermod -aG docker "$ADMIN_USER"
echo "[phase:${PHASE_NAME}] Added $ADMIN_USER to docker group."

# ---- AWS CLI v2 ----
if ! command -v aws &>/dev/null; then
    echo "[phase:${PHASE_NAME}] Installing AWS CLI v2..."
    AWSCLI_ARCH="$(uname -m)"
    case "$AWSCLI_ARCH" in
        x86_64)        AWSCLI_ZIP="awscli-exe-linux-x86_64.zip" ;;
        aarch64|arm64) AWSCLI_ZIP="awscli-exe-linux-aarch64.zip" ;;
        *)
            echo "[phase:${PHASE_NAME}] ERROR: Unsupported architecture for AWS CLI v2: $AWSCLI_ARCH" >&2
            exit 1
            ;;
    esac
    curl -fsSL "https://awscli.amazonaws.com/${AWSCLI_ZIP}" -o "/tmp/awscliv2.zip"
    apt-get install -y unzip
    unzip -q /tmp/awscliv2.zip -d /tmp
    /tmp/aws/install --update
    rm -rf /tmp/aws /tmp/awscliv2.zip
    echo "[phase:${PHASE_NAME}] Installed AWS CLI: $(aws --version)"
fi

# ---- Essential host tools ----
echo "[phase:${PHASE_NAME}] Checking essential host tools..."
ESSENTIAL_PKGS=()
command -v git   &>/dev/null || ESSENTIAL_PKGS+=(git)
if command -v python3 &>/dev/null; then
    python3 -c 'import yaml' &>/dev/null || ESSENTIAL_PKGS+=(python3-yaml)
else
    ESSENTIAL_PKGS+=(python3 python3-yaml)
fi
command -v jq       &>/dev/null || ESSENTIAL_PKGS+=(jq)
command -v crontab  &>/dev/null || ESSENTIAL_PKGS+=(cron)
command -v psql     &>/dev/null || ESSENTIAL_PKGS+=(postgresql-client)

if (( ${#ESSENTIAL_PKGS[@]} > 0 )); then
    apt-get update -y
    apt-get install -y "${ESSENTIAL_PKGS[@]}"
    echo "[phase:${PHASE_NAME}] Installed: ${ESSENTIAL_PKGS[*]}"
else
    echo "[phase:${PHASE_NAME}] All essential tools already present."
fi

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
