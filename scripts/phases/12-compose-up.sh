#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"


PHASE_NAME="12-compose-up"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
COMPOSE_ENV="$DATA_DIR/.setup/compose-env.sh"


echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

# Source secrets.
if [[ ! -f "$SECRETS_ENV" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $SECRETS_ENV not found — run 10-fetch-secrets.sh first" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$SECRETS_ENV"

echo "[phase:${PHASE_NAME}] Loading bootstrap Slack channel config..."
load_instance_env
hydrate_slack_channel_env "$COMPOSE_ENV"
write_instance_env || {
    echo "[phase:${PHASE_NAME}] ERROR: failed to write $(instance_env_path)" >&2
    exit 1
}
echo "[phase:${PHASE_NAME}] Slack channels: standup=${SLACK_STANDUP_CHANNEL:-unset} ops=${SLACK_OPS_CHANNEL:-unset}"
if is_blank_or_placeholder "${SLACK_OPS_CHANNEL:-}"; then
    echo "[phase:${PHASE_NAME}] WARNING: Slack channel IDs still unset; compose-env will fail later." >&2
    echo "[phase:${PHASE_NAME}] Check $(instance_env_path) and ${TS_HARNESS_CONFIG_DIR}/ts-harness-runtime.yaml." >&2
fi

export TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"

echo "[phase:${PHASE_NAME}] Starting containers (memory_backend=${MEMORY_BACKEND:-unset})..."

resolve_cloud_neutral_env

# Build COMPOSE_FILES and resolve DB connection vars. TS Harness owns the
# full compose stack after cutover; no upstream runtime compose is required.
COMPOSE_FILES=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.memory-api.yml")

if is_managed_memory_backend; then
    if [[ -z "${MANAGED_DB_CREDENTIAL_REF:-}" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: memory_backend=${MEMORY_BACKEND} but MANAGED_DB_CREDENTIAL_REF (or RDS_SECRET_ARN) is empty" >&2
        exit 1
    fi
    if [[ -z "${MANAGED_DB_HOST:-}" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: memory_backend=${MEMORY_BACKEND} but MANAGED_DB_HOST (or RDS_ENDPOINT) is empty" >&2
        exit 1
    fi
    echo "[phase:${PHASE_NAME}] Fetching managed DB credentials (ref=${MANAGED_DB_CREDENTIAL_REF})..."
    ensure_cloud_adapter
    MANAGED_DB_JSON="$(cloud_fetch_managed_db_credentials "$MANAGED_DB_CREDENTIAL_REF")"
    export MANAGED_DB_HOST
    export MANAGED_DB_PORT="${MANAGED_DB_PORT:-5432}"
    normalize_db_host_port MANAGED_DB_HOST MANAGED_DB_PORT
    MANAGED_DB_USER="$(echo "$MANAGED_DB_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])')"
    MANAGED_DB_PASSWORD="$(echo "$MANAGED_DB_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
    export MANAGED_DB_USER MANAGED_DB_PASSWORD
    export MANAGED_DB_NAME="${MANAGED_DB_NAME:-ts_harness}"
    export TS_HARNESS_DB_HOST="$MANAGED_DB_HOST"
    export TS_HARNESS_DB_PORT="$MANAGED_DB_PORT"
    export TS_HARNESS_DB_USER="$MANAGED_DB_USER"
    export TS_HARNESS_DB_PASSWORD="$MANAGED_DB_PASSWORD"
    export TS_HARNESS_DB_NAME="$MANAGED_DB_NAME"
    COMPOSE_FILES+=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.managed-db.yml")
else
    export TS_HARNESS_DB_HOST="ts-harness-db"
    export TS_HARNESS_DB_PORT="5432"
    export TS_HARNESS_DB_USER="ts_harness"
    export TS_HARNESS_DB_NAME="ts_harness_memory"
    COMPOSE_FILES+=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.local-db.yml")
fi

# Run preflight memory env check.
PREFLIGHT="$SCRIPT_DIR/preflight-memory-env.sh"
if [[ ! -f "$PREFLIGHT" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $PREFLIGHT not found" >&2
    exit 1
fi
chmod +x "$PREFLIGHT"
MEMORY_BACKEND="$MEMORY_BACKEND" SECRETS_PREFIX="$SECRETS_PREFIX" \
    MULTI_AGENT_ENABLED="${MULTI_AGENT_ENABLED:-false}" \
    EMBEDDINGS_PROVIDER="${EMBEDDINGS_PROVIDER:-openai}" \
    TS_HARNESS_DB_HOST="${TS_HARNESS_DB_HOST:-}" \
    TS_HARNESS_DB_PORT="${TS_HARNESS_DB_PORT:-}" \
    TS_HARNESS_DB_USER="${TS_HARNESS_DB_USER:-}" \
    TS_HARNESS_DB_NAME="${TS_HARNESS_DB_NAME:-}" \
    TS_HARNESS_DB_PASSWORD="${TS_HARNESS_DB_PASSWORD:-}" \
    "$PREFLIGHT"

AGENT_RUNTIME_COMPOSE="$ENTERPRISE_DOCKER_DIR/docker-compose.agent-runtime.yml"
if [[ -f "$AGENT_RUNTIME_COMPOSE" ]]; then
    COMPOSE_FILES+=(-f "$AGENT_RUNTIME_COMPOSE")
    echo "[phase:${PHASE_NAME}] Including TS Harness agent-runtime sidecar"
else
    echo "[phase:${PHASE_NAME}] ERROR: agent-runtime compose file not found at $AGENT_RUNTIME_COMPOSE" >&2
    exit 1
fi

ADMIN_UI_COMPOSE="$ENTERPRISE_DOCKER_DIR/docker-compose.admin-ui.yml"
if [[ -f "$ADMIN_UI_COMPOSE" ]]; then
    COMPOSE_FILES+=(-f "$ADMIN_UI_COMPOSE")
    echo "[phase:${PHASE_NAME}] Including admin console (admin-ui on port 8080)"
else
    echo "[phase:${PHASE_NAME}] ERROR: admin-ui compose file not found at $ADMIN_UI_COMPOSE" >&2
    exit 1
fi

if [[ "${WORKFLOW_RUNNER_ENABLED:-false}" == "true" ]]; then
    WORKFLOW_RUNNER_COMPOSE="$ENTERPRISE_DOCKER_DIR/docker-compose.workflow-runner.yml"
    if [[ -f "$WORKFLOW_RUNNER_COMPOSE" ]]; then
        COMPOSE_FILES+=(-f "$WORKFLOW_RUNNER_COMPOSE")
        echo "[phase:${PHASE_NAME}] Workflow runner enabled: including workflow-runner sidecar"
    else
        echo "[phase:${PHASE_NAME}] WARNING: workflow-runner compose file not found at $WORKFLOW_RUNNER_COMPOSE"
    fi
    if [[ "${CODING_JOBS_ENABLED:-false}" == "true" ]]; then
        CODING_JOBS_COMPOSE="$ENTERPRISE_DOCKER_DIR/docker-compose.coding-jobs.yml"
        if [[ -f "$CODING_JOBS_COMPOSE" ]]; then
            COMPOSE_FILES+=(-f "$CODING_JOBS_COMPOSE")
            echo "[phase:${PHASE_NAME}] Coding jobs enabled: including coding-launcher and the worker image"
        else
            echo "[phase:${PHASE_NAME}] WARNING: coding-jobs compose file not found at $CODING_JOBS_COMPOSE"
        fi
    fi
fi

if [[ "${DEPLOYMENT_PROFILE:-single_host}" == "scale_out" ]]; then
    SCALE_OUT_COMPOSE="$ENTERPRISE_DOCKER_DIR/docker-compose.scale-out.yml"
    if [[ -f "$SCALE_OUT_COMPOSE" ]]; then
        COMPOSE_FILES+=(-f "$SCALE_OUT_COMPOSE")
        echo "[phase:${PHASE_NAME}] Deployment profile scale_out: including replicated services"
    else
        echo "[phase:${PHASE_NAME}] WARNING: scale-out compose file not found at $SCALE_OUT_COMPOSE"
    fi
    if (( ${AGENT_RUNTIME_REPLICAS:-2} >= 2 )); then
        REPLICAS_COMPOSE="$ENTERPRISE_DOCKER_DIR/docker-compose.replicas.yml"
        COMPOSE_VERSION="$(docker compose version --short 2>/dev/null || echo 0)"
        if ! printf '2.24.4\n%s\n' "${COMPOSE_VERSION#v}" | sort -V -C; then
            echo "[phase:${PHASE_NAME}] ERROR: runtime replicas need Docker Compose 2.24.4+, found ${COMPOSE_VERSION}" >&2
            exit 1
        fi
        if is_blank_or_placeholder "$(session_db_url)"; then
            echo "[phase:${PHASE_NAME}] ERROR: runtime replicas need a session database (TS_HARNESS_SESSION_DB_URL)" >&2
            exit 1
        fi
        COMPOSE_FILES+=(-f "$REPLICAS_COMPOSE")
        echo "[phase:${PHASE_NAME}] Including a second agent-runtime and memory-api behind PgBouncer"
    fi
fi

PHASE_FINGERPRINT="$(calculate_phase_fingerprint "${COMPOSE_FILES[@]}" "$SECRETS_ENV" "$SCRIPT_DIR/preflight-memory-env.sh")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

# agent-runtime compose references env_file before phase 14 runs; seed from secrets.env.
RUNTIME_ENV="$TS_HARNESS_CONFIG_DIR/.env"
sync_google_workspace_sa_json "$GOOGLE_WORKSPACE_SA_JSON_PATH"
ensure_google_workspace_sa_mount_file
export TS_HARNESS_TOOLS_ROOT_HOST="${TS_HARNESS_TOOLS_ROOT_HOST:-${DATA_DIR}/.ts-harness/tools}"
export TS_HARNESS_TOOLS_ROOT="/tools"
mkdir -p "$TS_HARNESS_TOOLS_ROOT_HOST"
write_runtime_env_file "$RUNTIME_ENV"
chown "$ADMIN_USER:$ADMIN_USER" "$RUNTIME_ENV"
echo "[phase:${PHASE_NAME}] Wrote runtime env at $RUNTIME_ENV"

CRON_CONFIG="$ENTERPRISE_ASSETS_DIR/config/runtime-cron-jobs.json"
if [[ -d "$CRON_CONFIG" ]]; then
    echo "[phase:${PHASE_NAME}] Removing directory at $CRON_CONFIG (Docker bind-mount placeholder)." >&2
    rm -rf "$CRON_CONFIG"
fi
mkdir -p "$(dirname "$CRON_CONFIG")"
if [[ ! -f "$CRON_CONFIG" ]]; then
    echo '{"jobs":[]}' > "$CRON_CONFIG"
    chown "$ADMIN_USER:$ADMIN_USER" "$CRON_CONFIG"
fi

RUNTIME_CONFIG="$TS_HARNESS_CONFIG_DIR/ts-harness-runtime.yaml"
if [[ ! -f "$RUNTIME_CONFIG" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: Rendered runtime config not found: $RUNTIME_CONFIG" >&2
    echo "[phase:${PHASE_NAME}] Run 11-agent-runtime-setup.sh before compose up." >&2
    exit 1
fi

# Use shared helper so compose interpolation gets TS_HARNESS_TOOLS_ROOT_HOST
# (bind-mount host tools -> /tools). A hand-rolled sudo env list previously omitted it.
cd "$ENTERPRISE_ASSETS_DIR"
echo "[phase:${PHASE_NAME}] Mounting coding tools from TS_HARNESS_TOOLS_ROOT_HOST=${TS_HARNESS_TOOLS_ROOT_HOST}"
run_docker_compose_as_admin up -d
echo "[phase:${PHASE_NAME}] Containers started."

# Persist compose file list and DB vars for Phases 13 and 14.
mkdir -p "$DATA_DIR/.setup"
if ! require_slack_channel_env "$PHASE_NAME"; then
    echo "[phase:${PHASE_NAME}] ERROR: Cannot write compose-env.sh without Slack channel IDs." >&2
    exit 1
fi
{
    echo "export TS_HARNESS_DB_HOST=$(printf '%q' "$TS_HARNESS_DB_HOST")"
    echo "export TS_HARNESS_DB_PORT=$(printf '%q' "$TS_HARNESS_DB_PORT")"
    echo "export TS_HARNESS_DB_USER=$(printf '%q' "$TS_HARNESS_DB_USER")"
    echo "export TS_HARNESS_DB_NAME=$(printf '%q' "$TS_HARNESS_DB_NAME")"
    echo "export TS_HARNESS_DB_PASSWORD=$(printf '%q' "$TS_HARNESS_DB_PASSWORD")"
    echo "export OPENAI_API_KEY=$(printf '%q' "${OPENAI_API_KEY:-}")"
    echo "export VOYAGE_API_KEY=$(printf '%q' "${VOYAGE_API_KEY:-}")"
    echo "export GEMINI_API_KEY=$(printf '%q' "${GEMINI_API_KEY:-}")"
    echo "export MISTRAL_API_KEY=$(printf '%q' "${MISTRAL_API_KEY:-}")"
    echo "export GATEWAY_TOKEN=$(printf '%q' "${GATEWAY_TOKEN:-}")"
    echo "export MEMORY_API_TOKEN=$(printf '%q' "${MEMORY_API_TOKEN:-}")"
    echo "export SANDBOX_WORKER_TOKEN=$(printf '%q' "${SANDBOX_WORKER_TOKEN:-}")"
    echo "export CODING_JOB_SECRET=$(printf '%q' "${CODING_JOB_SECRET:-}")"
    echo "export WORKFLOW_RUNNER_ENABLED=$(printf '%q' "${WORKFLOW_RUNNER_ENABLED:-false}")"
    echo "export WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS=$(printf '%q' "${WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS:-10}")"
    echo "export WORKFLOW_RUNNER_BATCH_SIZE=$(printf '%q' "${WORKFLOW_RUNNER_BATCH_SIZE:-5}")"
    echo "export WORKFLOW_RUNNER_LEASE_SECONDS=$(printf '%q' "${WORKFLOW_RUNNER_LEASE_SECONDS:-1800}")"
    echo "export WORKFLOW_RUNNER_STALE_POLICY=$(printf '%q' "${WORKFLOW_RUNNER_STALE_POLICY:-block}")"
    echo "export WORKFLOW_RUNNER_ADAPTER=$(printf '%q' "${WORKFLOW_RUNNER_ADAPTER:-gateway-http}")"
    echo "export CODING_JOBS_ENABLED=$(printf '%q' "${CODING_JOBS_ENABLED:-false}")"
    echo "export CODING_MAX_RUNNING=$(printf '%q' "${CODING_MAX_RUNNING:-3}")"
    echo "export CODING_LAUNCHER=$(printf '%q' "${CODING_LAUNCHER:-}")"
    echo "export DEPLOYMENT_PROFILE=$(printf '%q' "${DEPLOYMENT_PROFILE:-single_host}")"
    echo "export CODING_ECS_CLUSTER=$(printf '%q' "${CODING_ECS_CLUSTER:-}")"
    echo "export CODING_ECS_TASK_DEFINITION=$(printf '%q' "${CODING_ECS_TASK_DEFINITION:-}")"
    echo "export CODING_ECS_CONTAINER=$(printf '%q' "${CODING_ECS_CONTAINER:-coding-worker}")"
    echo "export CODING_ECS_SUBNETS=$(printf '%q' "${CODING_ECS_SUBNETS:-}")"
    echo "export CODING_ECS_SECURITY_GROUPS=$(printf '%q' "${CODING_ECS_SECURITY_GROUPS:-}")"
    echo "export AGENT_RUNTIME_URL=$(printf '%q' "${AGENT_RUNTIME_URL:-http://127.0.0.1:${GATEWAY_PORT:-18789}}")"
    echo "export TS_HARNESS_TOOLS_ROOT_HOST=$(printf '%q' "${TS_HARNESS_TOOLS_ROOT_HOST}")"
    echo "export TS_HARNESS_TOOLS_ROOT=$(printf '%q' "${TS_HARNESS_TOOLS_ROOT:-/tools}")"
    echo "export AGENT_TIMEZONE=$(printf '%q' "${AGENT_TIMEZONE:-UTC}")"
    echo "export SLACK_STANDUP_CHANNEL=$(printf '%q' "${SLACK_STANDUP_CHANNEL:-}")"
    echo "export SLACK_OPS_CHANNEL=$(printf '%q' "${SLACK_OPS_CHANNEL:-}")"
    echo "export SLACK_DEV_CHANNEL=$(printf '%q' "${SLACK_DEV_CHANNEL:-}")"
    echo "export SLACK_TEAM_CHANNEL=$(printf '%q' "${SLACK_TEAM_CHANNEL:-}")"
    echo "export SLACK_MARKETING_CHANNEL=$(printf '%q' "${SLACK_MARKETING_CHANNEL:-}")"
    echo "export SLACK_SALES_CHANNEL=$(printf '%q' "${SLACK_SALES_CHANNEL:-}")"
    # Serialize compose files as a newline-delimited list (one entry per token).
    echo "COMPOSE_FILES_SERIALIZED=$(printf '%q' "$(printf '%s\n' "${COMPOSE_FILES[@]}")")"
} > "$COMPOSE_ENV"
chmod 600 "$COMPOSE_ENV"

write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
# Post-deploy validation: run scripts/ops/post-deploy-smoke.sh manually after secrets are wired (not on boot).
echo "[phase:${PHASE_NAME}] Done."
