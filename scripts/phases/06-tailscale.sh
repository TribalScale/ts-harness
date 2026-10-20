#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"


PHASE_NAME="06-tailscale"
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

echo "[phase:${PHASE_NAME}] Installing Tailscale..."
if command -v tailscale &>/dev/null; then
    echo "[phase:${PHASE_NAME}] Tailscale already installed."
else
    curl -fsSL https://tailscale.com/install.sh | sh
    echo "[phase:${PHASE_NAME}] Tailscale installed."
fi

TAILSCALE_AUTHKEY=""
if command -v aws &>/dev/null; then
    TAILSCALE_AUTHKEY="$(fetch_secret "tailscale-authkey" true)"
fi

TS_FQDN=""
if [[ -n "${TAILSCALE_AUTHKEY//[[:space:]]/}" ]]; then
    tailscale up --authkey="$TAILSCALE_AUTHKEY" --accept-routes
    echo "[phase:${PHASE_NAME}] Tailscale authenticated and connected."

    # The admin console nginx proxies every gateway route, so the tailnet root
    # can serve the console; CONTROL_UI_ALLOWED_ORIGIN (phase 11) expects it here.
    tailscale serve --bg http://localhost:8080
    TS_FQDN="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    echo "[phase:${PHASE_NAME}] Tailscale HTTPS proxy enabled: https://${TS_FQDN}"
else
    echo "[phase:${PHASE_NAME}] No Tailscale auth key found; skipping auto-authentication."
fi

# Write TS_FQDN to a file so later runtime setup phases can read it.
mkdir -p "$DATA_DIR/.setup"
echo "$TS_FQDN" > "$DATA_DIR/.setup/tailscale-fqdn"

printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
