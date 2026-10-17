#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="16-release-gates"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
COMPOSE_ENV="$DATA_DIR/.setup/compose-env.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

source_required_env_files "$SECRETS_ENV" "$COMPOSE_ENV"
reconstruct_compose_files
write_instance_env

if is_blank_or_placeholder "${GATEWAY_TOKEN:-}"; then
    echo "[phase:${PHASE_NAME}] ERROR: GATEWAY_TOKEN is empty after sourcing secrets.env and compose-env.sh." >&2
    echo "[phase:${PHASE_NAME}] Re-run phase 10-fetch-secrets.sh and phase 14-channels-init.sh to refresh runtime .env." >&2
    exit 1
fi

PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$SCRIPT_DIR/release-gates.py" "$SECRETS_ENV" "$COMPOSE_ENV")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

SLACK_RELEASE_GATE_CHANNEL="${SLACK_RELEASE_GATE_CHANNEL:-${SLACK_OPS_CHANNEL:-}}"
if [[ -z "${SLACK_RELEASE_GATE_CHANNEL//[[:space:]]/}" ]]; then
    echo "[phase:${PHASE_NAME}] WARNING: SLACK_RELEASE_GATE_CHANNEL not set; slack_round_trip gate will fail." >&2
fi

cd "$ENTERPRISE_ASSETS_DIR"
if ! wait_for_agent_runtime_health "$PHASE_NAME"; then
    exit 1
fi

echo "[phase:${PHASE_NAME}] Running live TS Harness release gates..."
if ! run_docker_compose_as_admin exec -T \
    -e AGENT_RUNTIME_URL=http://127.0.0.1:18789 \
    -e TS_HARNESS_CONFIG=/config/ts-harness-runtime.yaml \
    -e SLACK_RELEASE_GATE_CHANNEL="${SLACK_RELEASE_GATE_CHANNEL:-}" \
    agent-runtime python /app/scripts/release-gates.py; then
    echo "[phase:${PHASE_NAME}] ERROR: release gates failed." >&2
    exit 1
fi

mkdir -p "$DATA_DIR/.setup"
write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
