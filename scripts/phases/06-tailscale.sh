#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"
# Manual re-runs: TAILSCALE_HOSTNAME and TAILSCALE_TAGS persisted by the first bootstrap.
load_instance_env


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

# Pinned for stability: new Tailscale releases reach hosts only when this pin is raised
# (see docs/runbooks/rebuild-a-host.md#tailscale-version). Override with TAILSCALE_VERSION.
TAILSCALE_VERSION="${TAILSCALE_VERSION:-1.104.6}"
installed_version="$(tailscale version 2>/dev/null | head -1 || true)"
if [[ "$installed_version" == "$TAILSCALE_VERSION" ]]; then
    echo "[phase:${PHASE_NAME}] Tailscale ${TAILSCALE_VERSION} already installed."
else
    echo "[phase:${PHASE_NAME}] Installing Tailscale ${TAILSCALE_VERSION} (found: ${installed_version:-none})..."
    if ! command -v tailscale &>/dev/null; then
        # Adds Tailscale's apt repository (and installs the latest), then we pin below.
        curl -fsSL https://tailscale.com/install.sh | sh
    fi
    apt-mark unhold tailscale >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades "tailscale=${TAILSCALE_VERSION}"
    echo "[phase:${PHASE_NAME}] Tailscale ${TAILSCALE_VERSION} installed."
fi
apt-mark hold tailscale >/dev/null

TAILSCALE_AUTHKEY=""
if command -v aws &>/dev/null; then
    TAILSCALE_AUTHKEY="$(fetch_secret "tailscale-authkey" true)"
fi

TS_FQDN=""
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-}"
TAILSCALE_TAGS="${TAILSCALE_TAGS:-}"
if [[ -n "${TAILSCALE_AUTHKEY//[[:space:]]/}" ]]; then
    UP_ARGS=(--accept-routes)
    # A fixed machine name keeps the tailnet URL (console, OAuth callbacks, webhook) across rebuilds.
    [[ -n "$TAILSCALE_HOSTNAME" ]] && UP_ARGS+=(--hostname="$TAILSCALE_HOSTNAME")
    AUTHKEY="$TAILSCALE_AUTHKEY"
    if [[ "$TAILSCALE_AUTHKEY" == tskey-client-* ]]; then
        # OAuth client secret: never expires, but the machine must be tagged.
        TAILSCALE_TAGS="${TAILSCALE_TAGS:-tag:ts-harness}"
        [[ "$AUTHKEY" == *\?* ]] || AUTHKEY="${AUTHKEY}?ephemeral=false&preauthorized=true"
        if [[ -n "$TAILSCALE_HOSTNAME" ]]; then
            # Remove the previous host's offline machine so this one gets the name, not <name>-1.
            TS_OAUTH_CLIENT_SECRET="$TAILSCALE_AUTHKEY" python3 "${SCRIPT_DIR_LIB}/../ops/tailscale-stale-device.py" \
                --hostname "$TAILSCALE_HOSTNAME" || echo "[phase:${PHASE_NAME}] WARNING: stale-device cleanup failed; the machine may be named ${TAILSCALE_HOSTNAME}-1."
        fi
    fi
    [[ -n "$TAILSCALE_TAGS" ]] && UP_ARGS+=(--advertise-tags="$TAILSCALE_TAGS")
    if tailscale status --json 2>/dev/null | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("BackendState") == "Running" else 1)'; then
        # Already in the tailnet (phase re-run): adjust the name without re-authenticating.
        [[ -n "$TAILSCALE_HOSTNAME" ]] && tailscale set --hostname="$TAILSCALE_HOSTNAME"
        echo "[phase:${PHASE_NAME}] Tailscale already connected."
    else
        tailscale up --authkey="$AUTHKEY" "${UP_ARGS[@]}"
        echo "[phase:${PHASE_NAME}] Tailscale authenticated and connected."
    fi
    unset AUTHKEY

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
