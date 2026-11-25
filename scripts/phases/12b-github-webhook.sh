#!/usr/bin/env bash
# Expose the GitHub webhook through Tailscale Funnel and point the GitHub App at it.
#
# Runs when GITHUB_WEBHOOK_FUNNEL=true (install YAML github_app.webhook_funnel). Funnel serves
# only /webhooks/github and /webhooks/smoke (nightly smoke failure reports from GitHub Actions,
# docs/runbooks/nightly-smoke.md), on port 8443; the admin console on 443 stays tailnet-only. The App's
# webhook URL follows the host's Tailscale name, so a rebuilt host re-points it here.
# Never fails the bootstrap: problems are logged with what to do. See docs/runbooks/github-app.md.
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

PHASE_NAME="12b-github-webhook"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
FUNNEL_PORT="${GITHUB_WEBHOOK_FUNNEL_PORT:-8443}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

load_instance_env
resolve_cloud_neutral_env

if [[ "${GITHUB_WEBHOOK_FUNNEL:-false}" != "true" ]]; then
    echo "[phase:${PHASE_NAME}] GITHUB_WEBHOOK_FUNNEL is not true; skipping."
    exit 0
fi

TS_FQDN="$(cat "$DATA_DIR/.setup/tailscale-fqdn" 2>/dev/null || true)"
if [[ -z "$TS_FQDN" ]] || ! command -v tailscale &>/dev/null; then
    echo "[phase:${PHASE_NAME}] WARNING: Tailscale is not connected; webhook not exposed."
    exit 0
fi
URL="https://${TS_FQDN}:${FUNNEL_PORT}/webhooks/github"

PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$0" "$SCRIPT_DIR/github-app-webhook.py" "url:${URL}")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for ${URL}. Skipping."
    exit 0
fi

if ! tailscale funnel --bg --https="$FUNNEL_PORT" --set-path=/webhooks/github http://localhost:8080/webhooks/github; then
    echo "[phase:${PHASE_NAME}] WARNING: tailscale funnel failed. The tailnet policy must allow Funnel for this node (nodeAttrs: funnel)."
    exit 0
fi
echo "[phase:${PHASE_NAME}] Funnel serving ${URL}"
# Smoke reports authenticate with per-repository tokens; without SMOKE_REPORT_SECRET they get 503.
if ! tailscale funnel --bg --https="$FUNNEL_PORT" --set-path=/webhooks/smoke http://localhost:8080/webhooks/smoke; then
    echo "[phase:${PHASE_NAME}] WARNING: tailscale funnel for /webhooks/smoke failed; nightly smoke failures will not reach the harness."
fi

RUNTIME="$(docker ps -q --filter label=com.docker.compose.service=agent-runtime | head -1)"
if [[ -z "$RUNTIME" ]]; then
    echo "[phase:${PHASE_NAME}] WARNING: agent-runtime is not running; re-run this phase after compose up."
    exit 0
fi
status=0
for _ in 1 2 3 4 5 6; do
    status=0
    docker exec "$RUNTIME" python3 /app/scripts/github-app-webhook.py --url "$URL" || status=$?
    [[ "$status" -ne 1 ]] && break
    sleep 10
done
if [[ "$status" -ne 0 ]]; then
    echo "[phase:${PHASE_NAME}] WARNING: the GitHub App webhook needs attention (see ACTION lines above); re-run this phase after fixing it."
    exit 0
fi

mkdir -p "$DATA_DIR/.setup"
write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
