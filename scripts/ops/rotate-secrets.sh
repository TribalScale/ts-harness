#!/usr/bin/env bash
set -euo pipefail

# Re-fetch secrets from AWS Secrets Manager and recreate TS Harness runtime consumers.

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
PHASES_DIR="$SCRIPT_DIR/phases"
DATA_DIR="${DATA_DIR:-/vol/ts-harness-data}"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
COMPOSE_ENV="$DATA_DIR/.setup/compose-env.sh"
TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"
RUNTIME_ENV="$TS_HARNESS_CONFIG_DIR/.env"
ADMIN_USER="${ADMIN_USER:-artemis}"
ENTERPRISE_ASSETS_DIR="${ENTERPRISE_ASSETS_DIR:-/home/${ADMIN_USER}/enterprise-assets}"
ENTERPRISE_DOCKER_DIR="${ENTERPRISE_DOCKER_DIR:-$SCRIPT_DIR/../docker}"

export SCRIPT_DIR DATA_DIR TS_HARNESS_CONFIG_DIR ADMIN_USER ENTERPRISE_ASSETS_DIR ENTERPRISE_DOCKER_DIR

# shellcheck disable=SC1091
source "$PHASES_DIR/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"
ensure_cloud_adapter
export -f fetch_secret is_blank_or_placeholder resolve_ebs_data_device ensure_cloud_adapter
export -f cloud_fetch_secret cloud_fetch_managed_db_credentials 2>/dev/null || true
export -f _cloud_aws_bin 2>/dev/null || true

require_env "AWS_REGION"
require_env "SECRETS_PREFIX"
require_env "TS_HARNESS_CONFIG_DIR"
require_env "MEMORY_BACKEND"
require_env "EMBEDDINGS_PROVIDER"

clear_phase_marker() {
    local phase_name="$1"
    local phase_done="$DATA_DIR/.setup/phase-${phase_name}.done"
    # rm -f so a missing marker does not trip set -e via `[[ -f ]] && rm`.
    rm -f "$phase_done"
}

source_refreshed_secrets() {
    if [[ ! -f "$SECRETS_ENV" ]]; then
        echo "ERROR: $SECRETS_ENV not found after re-running fetch-secrets phase" >&2
        exit 1
    fi
    # shellcheck disable=SC1090
    source "$SECRETS_ENV"
    export GATEWAY_TOKEN MEMORY_API_TOKEN SANDBOX_WORKER_TOKEN
    export SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_SIGNING_SECRET
    export OPENAI_API_KEY VOYAGE_API_KEY GEMINI_API_KEY MISTRAL_API_KEY ANTHROPIC_API_KEY
    export TS_HARNESS_DB_PASSWORD
    export CODING_BACKEND_API_KEY CODING_BACKEND_PROVIDER CODING_BACKEND_MODEL GITHUB_TOKEN
    export GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET GOOGLE_OAUTH_CALLBACK_URL
    export AGENT_CREDENTIALS_KMS_KEY_ID GOOGLE_WORKSPACE_SA_JSON_PATH
}

persist_runtime_secret_keys() {
    # Must use the shared writer — a hand-rolled key list previously dropped
    # CODING_BACKEND_* / GITHUB_TOKEN and broke coding_backend_readiness after rotation.
    export TS_HARNESS_TOOLS_ROOT="${TS_HARNESS_TOOLS_ROOT:-/tools}"
    write_runtime_env_file "$RUNTIME_ENV"
    echo "Updated runtime secret env at $RUNTIME_ENV"
}

reconstruct_enterprise_compose_files() {
    COMPOSE_FILES=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.memory-api.yml" -f "$ENTERPRISE_DOCKER_DIR/docker-compose.agent-runtime.yml")
    [[ "${WORKFLOW_RUNNER_ENABLED:-false}" == "true" && -f "$ENTERPRISE_DOCKER_DIR/docker-compose.workflow-runner.yml" ]] && \
        COMPOSE_FILES+=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.workflow-runner.yml")
}

refresh_compose_env() {
    reconstruct_enterprise_compose_files
    source_refreshed_secrets
    mkdir -p "$DATA_DIR/.setup"
    if [[ -f "$COMPOSE_ENV" ]]; then
        sync_compose_db_password "$COMPOSE_ENV" "$TS_HARNESS_DB_PASSWORD"
    else
        echo "WARN: $COMPOSE_ENV missing; writing minimal compose env (run phase 12 for full file)" >&2
        {
            echo "export TS_HARNESS_DB_PASSWORD=$(printf '%q' "${TS_HARNESS_DB_PASSWORD:-}")"
            echo "export OPENAI_API_KEY=$(printf '%q' "${OPENAI_API_KEY:-}")"
            echo "export GATEWAY_TOKEN=$(printf '%q' "${GATEWAY_TOKEN:-}")"
            echo "export MEMORY_API_TOKEN=$(printf '%q' "${MEMORY_API_TOKEN:-}")"
            echo "export SANDBOX_WORKER_TOKEN=$(printf '%q' "${SANDBOX_WORKER_TOKEN:-}")"
            echo "export AGENT_RUNTIME_URL=$(printf '%q' "${AGENT_RUNTIME_URL:-http://127.0.0.1:18789}")"
            echo "COMPOSE_FILES_SERIALIZED=$(printf '%q' "$(printf '%s\n' "${COMPOSE_FILES[@]}")")"
        } > "$COMPOSE_ENV"
        chmod 600 "$COMPOSE_ENV"
    fi
}

recreate_secret_consumers() {
    cd "$ENTERPRISE_ASSETS_DIR"
    docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate agent-runtime
    echo "agent-runtime recreated."
    docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate memory-api
    echo "memory-api recreated."
    docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate sandbox-worker
    echo "sandbox-worker recreated."
    if [[ "${WORKFLOW_RUNNER_ENABLED:-false}" == "true" ]]; then
        docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate workflow-runner
        echo "workflow-runner recreated."
    fi
}

echo "=== TS Harness Secret Rotation ==="
clear_phase_marker "10-fetch-secrets"
export AWS_REGION SECRETS_PREFIX DATA_DIR TS_HARNESS_CONFIG_DIR MEMORY_BACKEND EMBEDDINGS_PROVIDER
bash "$PHASES_DIR/10-fetch-secrets.sh"
source_refreshed_secrets
persist_runtime_secret_keys
refresh_compose_env
# shellcheck disable=SC1090
source "$COMPOSE_ENV"
reconstruct_enterprise_compose_files
recreate_secret_consumers

echo ""
echo "=== Secret rotation complete ==="
echo "Verify with: docker compose logs --tail=20 agent-runtime"
