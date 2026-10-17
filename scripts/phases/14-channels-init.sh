#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"


PHASE_NAME="14-channels-init"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
COMPOSE_ENV="$DATA_DIR/.setup/compose-env.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

source_required_env_files "$SECRETS_ENV" "$COMPOSE_ENV"
reconstruct_compose_files

export TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"
RUNTIME_ENV="$TS_HARNESS_CONFIG_DIR/.env"
PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$SECRETS_ENV" "$COMPOSE_ENV" "$ENTERPRISE_ASSETS_DIR/config/ts-harness-runtime.yaml.tmpl")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

echo "[phase:${PHASE_NAME}] Writing runtime channel/provider environment..."
sync_google_workspace_sa_json "$GOOGLE_WORKSPACE_SA_JSON_PATH"
ensure_google_workspace_sa_mount_file
export TS_HARNESS_TOOLS_ROOT_HOST="${TS_HARNESS_TOOLS_ROOT_HOST:-${DATA_DIR}/.ts-harness/tools}"
export TS_HARNESS_TOOLS_ROOT="/tools"
write_runtime_env_file "$RUNTIME_ENV"
for key in \
    GATEWAY_TOKEN \
    SLACK_BOT_TOKEN \
    SLACK_APP_TOKEN \
    SLACK_SIGNING_SECRET \
    DISCORD_WEBHOOK_URL \
    TELEGRAM_BOT_TOKEN \
    OPENAI_API_KEY \
    VOYAGE_API_KEY \
    GEMINI_API_KEY \
    MISTRAL_API_KEY \
    GOOGLE_OAUTH_CLIENT_ID \
    GOOGLE_OAUTH_CLIENT_SECRET \
    GOOGLE_OAUTH_CALLBACK_URL \
    AGENT_CREDENTIALS_KMS_KEY_ID \
    GOOGLE_WORKSPACE_SA_JSON_PATH \
    TS_HARNESS_SKIP_GOOGLE_PROBE \
    CODING_BACKEND_API_KEY \
    CODING_BACKEND_PROVIDER \
    CODING_BACKEND_MODEL \
    GITHUB_TOKEN \
    TS_HARNESS_TOOLS_ROOT; do
    if grep -q "^${key}=" "$RUNTIME_ENV" 2>/dev/null; then
        echo "[phase:${PHASE_NAME}] ${key}=present"
    else
        case "$key" in
            CODING_BACKEND_API_KEY|CODING_BACKEND_PROVIDER|GITHUB_TOKEN)
                echo "[phase:${PHASE_NAME}] ${key}=missing (not written to runtime .env)" >&2
                ;;
        esac
    fi
done

echo "[phase:${PHASE_NAME}] Restarting agent-runtime to pick up channel/provider env."
cd "$ENTERPRISE_ASSETS_DIR"
recreate_ts_harness_services agent-runtime memory-api
if ! wait_for_agent_runtime_health "$PHASE_NAME"; then
    exit 1
fi

mkdir -p "$DATA_DIR/.setup"
write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
