#!/bin/bash
# Sourced by phase scripts that need shared utility functions.
# Requires: CLOUD_REGION/AWS_REGION, SECRETS_PREFIX exported by the orchestrator.

# Resolve cloud-neutral names from legacy AWS aliases (and backfill legacy from neutral).
# Prefer neutral names when both are set; emit deprecation notices for legacy-only usage.
resolve_cloud_neutral_env() {
    local _legacy_notice=0

    : "${CLOUD_PROVIDER:=aws}"
    export CLOUD_PROVIDER

    if [[ -z "${CLOUD_REGION:-}" && -n "${AWS_REGION:-}" ]]; then
        CLOUD_REGION="$AWS_REGION"
        _legacy_notice=1
    fi
    : "${CLOUD_REGION:=${AWS_REGION:-us-east-1}}"
    export CLOUD_REGION
    if [[ -z "${AWS_REGION:-}" ]]; then
        AWS_REGION="$CLOUD_REGION"
    fi
    export AWS_REGION

    if [[ -z "${MANAGED_DB_HOST:-}" && -n "${RDS_ENDPOINT:-}" ]]; then
        MANAGED_DB_HOST="$RDS_ENDPOINT"
        _legacy_notice=1
    fi
    if [[ -z "${MANAGED_DB_PORT:-}" && -n "${TS_HARNESS_DB_PORT:-}" ]]; then
        MANAGED_DB_PORT="$TS_HARNESS_DB_PORT"
    fi
    : "${MANAGED_DB_PORT:=5432}"
    if [[ -z "${MANAGED_DB_NAME:-}" && -n "${RDS_DB_NAME:-}" ]]; then
        MANAGED_DB_NAME="$RDS_DB_NAME"
        _legacy_notice=1
    fi
    if [[ -z "${MANAGED_DB_USER:-}" && -n "${TS_HARNESS_DB_USER:-}" ]]; then
        MANAGED_DB_USER="$TS_HARNESS_DB_USER"
    fi
    if [[ -z "${MANAGED_DB_PASSWORD:-}" && -n "${TS_HARNESS_DB_PASSWORD:-}" ]]; then
        MANAGED_DB_PASSWORD="$TS_HARNESS_DB_PASSWORD"
    fi
    if [[ -z "${MANAGED_DB_CREDENTIAL_REF:-}" && -n "${RDS_SECRET_ARN:-}" ]]; then
        MANAGED_DB_CREDENTIAL_REF="$RDS_SECRET_ARN"
        _legacy_notice=1
    fi
    export MANAGED_DB_HOST="${MANAGED_DB_HOST:-}"
    export MANAGED_DB_PORT
    export MANAGED_DB_NAME="${MANAGED_DB_NAME:-}"
    export MANAGED_DB_USER="${MANAGED_DB_USER:-}"
    export MANAGED_DB_PASSWORD="${MANAGED_DB_PASSWORD:-}"
    export MANAGED_DB_CREDENTIAL_REF="${MANAGED_DB_CREDENTIAL_REF:-}"

    # Backfill legacy RDS_* for AWS boot paths that still read them.
    if [[ -z "${RDS_ENDPOINT:-}" && -n "${MANAGED_DB_HOST:-}" ]]; then
        RDS_ENDPOINT="$MANAGED_DB_HOST"
    fi
    if [[ -z "${RDS_DB_NAME:-}" && -n "${MANAGED_DB_NAME:-}" ]]; then
        RDS_DB_NAME="$MANAGED_DB_NAME"
    fi
    if [[ -z "${RDS_SECRET_ARN:-}" && -n "${MANAGED_DB_CREDENTIAL_REF:-}" ]]; then
        RDS_SECRET_ARN="$MANAGED_DB_CREDENTIAL_REF"
    fi
    export RDS_ENDPOINT="${RDS_ENDPOINT:-}"
    export RDS_DB_NAME="${RDS_DB_NAME:-}"
    export RDS_SECRET_ARN="${RDS_SECRET_ARN:-}"

    if [[ -z "${CREDENTIALS_ENCRYPTION_KEY_ID:-}" && -n "${AGENT_CREDENTIALS_KMS_KEY_ID:-}" ]]; then
        CREDENTIALS_ENCRYPTION_KEY_ID="$AGENT_CREDENTIALS_KMS_KEY_ID"
        _legacy_notice=1
    fi
    export CREDENTIALS_ENCRYPTION_KEY_ID="${CREDENTIALS_ENCRYPTION_KEY_ID:-}"
    if [[ -z "${AGENT_CREDENTIALS_KMS_KEY_ID:-}" && -n "${CREDENTIALS_ENCRYPTION_KEY_ID:-}" ]]; then
        AGENT_CREDENTIALS_KMS_KEY_ID="$CREDENTIALS_ENCRYPTION_KEY_ID"
    fi
    export AGENT_CREDENTIALS_KMS_KEY_ID="${AGENT_CREDENTIALS_KMS_KEY_ID:-}"

    export INSTANCE_RESOURCE_ID="${INSTANCE_RESOURCE_ID:-}"

    if (( _legacy_notice == 1 )) && [[ "${CLOUD_NEUTRAL_ENV_VERBOSE:-0}" == "1" ]]; then
        echo "[env] NOTE: resolved legacy AWS aliases into cloud-neutral names (CLOUD_REGION/MANAGED_DB_*/CREDENTIALS_ENCRYPTION_KEY_ID)." >&2
    fi
}

is_managed_memory_backend() {
    case "${MEMORY_BACKEND:-}" in
        rds|managed) return 0 ;;
        *) return 1 ;;
    esac
}

# Load scripts/cloud/adapter.sh once (provider selected by CLOUD_PROVIDER).
ensure_cloud_adapter() {
    # Parent shells (e.g. rotate-secrets) may export -f cloud_fetch_secret and
    # CLOUD_ADAPTER_LOADED=1 without exporting provider helpers like _cloud_aws_bin.
    # Treat that as unloaded so the child re-sources a complete adapter.
    if [[ "${CLOUD_ADAPTER_LOADED:-0}" == "1" ]] && declare -F cloud_fetch_secret >/dev/null 2>&1; then
        if [[ "${CLOUD_PROVIDER:-aws}" != "aws" ]] || declare -F _cloud_aws_bin >/dev/null 2>&1; then
            return 0
        fi
    fi
    local _common_dir _adapter
    _common_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    _adapter="$(cd "${_common_dir}/../../cloud" && pwd)/adapter.sh"
    if [[ ! -f "$_adapter" ]]; then
        echo "ERROR: cloud adapter not found at ${_adapter}" >&2
        return 1
    fi
    # shellcheck disable=SC1090
    source "$_adapter"
}

# optional=true: ResourceNotFound logs WARNING and returns empty.
# optional=false: ResourceNotFound fails closed via cloud adapter (exit 1).
fetch_secret() {
    local secret_id="$1"
    local optional="${2:-false}"
    local mode="required"
    local result ec=0
    [[ "$optional" == "true" ]] && mode="optional"
    ensure_cloud_adapter
    result="$(cloud_fetch_secret "$secret_id" "$mode")" || ec=$?
    if (( ec == 0 )); then
        printf '%s' "$result"
        return 0
    fi
    exit 1
}

# Empty/whitespace or unsubstituted Terraform token (__TF_...) — treats incomplete renders as invalid.
is_blank_or_placeholder() {
    local v="${1:-}"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    [[ -z "$v" ]] && return 0
    [[ "$v" == *__TF_*__* ]] && return 0
    return 1
}

require_env() {
    local name="$1"
    if [[ -z "${!name:-}" ]]; then
        echo "ERROR: Required environment variable is not set: $name" >&2
        exit 1
    fi
}

write_env_var() {
    local key="$1" value="${2:-}"
    printf '%s=%q\n' "$key" "$value"
}

write_dotenv_var() {
    local key="$1" value="${2:-}" escaped
    escaped="$value"
    escaped="${escaped//\\/\\\\}"
    escaped="${escaped//$'\r'/}"
    escaped="${escaped//$'\n'/\\n}"
    escaped="${escaped//\"/\\\"}"
    printf '%s="%s"\n' "$key" "$escaped"
}

GOOGLE_WORKSPACE_SA_CONTAINER_PATH="/config/google-workspace-sa.json"

google_workspace_sa_host_path() {
    echo "${TS_HARNESS_CONFIG_DIR:-${DATA_DIR}/.ts-harness}/google-workspace-sa.json"
}

sync_google_workspace_sa_json() {
    local source="${1:-${GOOGLE_WORKSPACE_SA_JSON_PATH:-}}"
    local dest
    dest="$(google_workspace_sa_host_path)"
    if [[ -z "$source" || ! -f "$source" ]]; then
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    python3 - "$source" "$dest" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
dest = Path(sys.argv[2])
raw = source.read_text(encoding="utf-8").replace("\xa0", " ")
data = json.loads(raw)
dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"[sync] google workspace SA JSON -> {dest}")
PY
    chmod 600 "$dest"
}

ensure_google_workspace_sa_mount_file() {
    local dest
    dest="$(google_workspace_sa_host_path)"
    mkdir -p "$(dirname "$dest")"
    if [[ ! -f "$dest" ]]; then
        echo '{}' > "$dest"
        chmod 600 "$dest"
    fi
}

write_runtime_env_file() {
    local env_file="$1"
    local key value

    mkdir -p "$(dirname "$env_file")"
    : > "$env_file"
    chmod 600 "$env_file"

    for key in \
        GATEWAY_TOKEN \
        MEMORY_API_TOKEN \
        SANDBOX_WORKER_TOKEN \
        SLACK_BOT_TOKEN \
        SLACK_APP_TOKEN \
        SLACK_SIGNING_SECRET \
        DISCORD_WEBHOOK_URL \
        TELEGRAM_BOT_TOKEN \
        OPENAI_API_KEY \
        ANTHROPIC_API_KEY \
        VOYAGE_API_KEY \
        GEMINI_API_KEY \
        MISTRAL_API_KEY \
        GOOGLE_OAUTH_CLIENT_ID \
        GOOGLE_OAUTH_CLIENT_SECRET \
        GOOGLE_OAUTH_CALLBACK_URL \
        AGENT_CREDENTIALS_KMS_KEY_ID \
        CREDENTIALS_ENCRYPTION_KEY_ID \
        TS_HARNESS_SKIP_GOOGLE_PROBE \
        CODING_BACKEND_API_KEY \
        CODING_BACKEND_PROVIDER \
        CODING_BACKEND_MODEL \
        GITHUB_TOKEN \
        TS_HARNESS_TOOLS_ROOT \
        TS_HARNESS_DB_HOST \
        TS_HARNESS_DB_PORT \
        TS_HARNESS_DB_USER \
        TS_HARNESS_DB_NAME \
        TS_HARNESS_DB_PASSWORD \
        TS_HARNESS_PROGRAM_ACCESS \
        TS_HARNESS_SCIM_TOKEN \
        AUTH0_MGMT_DOMAIN \
        AUTH0_MGMT_CLIENT_ID \
        AUTH0_MGMT_CLIENT_SECRET \
        MS_GRAPH_TENANT_ID \
        MS_GRAPH_CLIENT_ID \
        MS_GRAPH_CLIENT_SECRET \
        GOOGLE_DIRECTORY_ADMIN_SUBJECT \
        GOOGLE_DIRECTORY_CUSTOMER \
        CLOUD_PROVIDER \
        CLOUD_REGION \
        SECRETS_PREFIX \
        CODING_ECS_CLUSTER \
        CODING_EGRESS_PROXY_SERVICE \
        CODING_EGRESS_DOMAINS_PARAMETER \
        CODING_EGRESS_PROXY_URL \
        PGSSLMODE; do
        value="${!key:-}"
        if [[ -n "${value//[[:space:]]/}" ]]; then
            write_dotenv_var "$key" "$value" >> "$env_file"
        fi
    done

    if [[ -f "$(google_workspace_sa_host_path)" ]]; then
        write_dotenv_var GOOGLE_WORKSPACE_SA_JSON_PATH "$GOOGLE_WORKSPACE_SA_CONTAINER_PATH" >> "$env_file"
    fi
}

enterprise_assets_commit() {
    if [[ -n "${ENTERPRISE_ASSETS_DIR:-}" && -d "$ENTERPRISE_ASSETS_DIR/.git" ]]; then
        git -C "$ENTERPRISE_ASSETS_DIR" rev-parse HEAD 2>/dev/null || echo unknown
    else
        echo unknown
    fi
}

calculate_phase_fingerprint() {
    local assets_commit
    assets_commit="$(enterprise_assets_commit)"
    ENTERPRISE_ASSETS_COMMIT="$assets_commit" python3 - "$@" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

digest = hashlib.sha256()

for raw in sys.argv[1:]:
    path = Path(raw)
    if not path.exists():
        digest.update(f"MISSING:{raw}\n".encode())
        continue
    if path.is_file():
        digest.update(f"FILE:{path}\n".encode())
        digest.update(path.read_bytes())
        continue
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        if ".git" in child.parts or "__pycache__" in child.parts:
            continue
        rel = child.relative_to(path)
        digest.update(f"FILE:{raw}/{rel}\n".encode())
        digest.update(child.read_bytes())

digest.update(f"AGENT_RUNTIME_VERSION:{os.environ.get('AGENT_RUNTIME_VERSION', '')}\n".encode())
digest.update(f"ASSETS_COMMIT:{os.environ.get('ENTERPRISE_ASSETS_COMMIT', '')}\n".encode())
print(digest.hexdigest())
PY
}

phase_marker_value() {
    local marker="$1" key="$2"
    grep "^${key}=" "$marker" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

phase_marker_matches() {
    local marker="$1" fingerprint="$2"
    [[ -f "$marker" ]] || return 1
    [[ "$(phase_marker_value "$marker" agent_runtime_version)" == "${AGENT_RUNTIME_VERSION:-}" ]] || return 1
    [[ "$(phase_marker_value "$marker" phase_fingerprint)" == "$fingerprint" ]] || return 1
    [[ "$(phase_marker_value "$marker" enterprise_assets_commit)" == "$(enterprise_assets_commit)" ]] || return 1
}

write_phase_marker() {
    local marker="$1" fingerprint="$2"
    mkdir -p "$(dirname "$marker")"
    {
        printf 'agent_runtime_version=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}"
        printf 'enterprise_assets_commit=%s\n' "$(enterprise_assets_commit)"
        printf 'phase_fingerprint=%s\n' "$fingerprint"
        printf 'completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$marker"
}

# Data volume resolution via cloud adapter (AWS: EBS/NVMe; GCP: Persistent Disk).
# Optional args: resource_id device_hint — defaults from DATA_DISK_* env.
resolve_ebs_data_device() {
    local resource_id="${1:-${DATA_DISK_RESOURCE_ID:-}}"
    local device_hint="${2:-${DATA_DISK_DEVICE_HINT:-/dev/sdf}}"
    ensure_cloud_adapter
    cloud_resolve_data_device "$resource_id" "$device_hint"
}

# RDS/terraform may supply hostname:port in a single string; split for psql/pg_isready.
normalize_db_host_port() {
    local host_var="$1" port_var="$2"
    local host="${!host_var:-}" port="${!port_var:-5432}"
    if [[ "$host" == *:* ]]; then
        port="${host##*:}"
        host="${host%%:*}"
    fi
    printf -v "$host_var" '%s' "$host"
    printf -v "$port_var" '%s' "$port"
}

SLACK_CHANNEL_KEYS=(
    SLACK_STANDUP_CHANNEL
    SLACK_OPS_CHANNEL
    SLACK_DEV_CHANNEL
    SLACK_TEAM_CHANNEL
    SLACK_MARKETING_CHANNEL
    SLACK_SALES_CHANNEL
)

INSTANCE_ENV_KEYS=(
    AGENT_TIMEZONE
    SLACK_STANDUP_CHANNEL
    SLACK_OPS_CHANNEL
    SLACK_DEV_CHANNEL
    SLACK_TEAM_CHANNEL
    SLACK_MARKETING_CHANNEL
    SLACK_SALES_CHANNEL
    APPROVED_SLACK_CHANNELS
    WORKFLOW_RUNNER_ENABLED
    WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS
    WORKFLOW_RUNNER_BATCH_SIZE
    WORKFLOW_RUNNER_LEASE_SECONDS
    WORKFLOW_RUNNER_STALE_POLICY
    CODING_JOBS_ENABLED
    CODING_MAX_RUNNING
    MEMORY_BACKEND
    CLOUD_PROVIDER
    CLOUD_REGION
    MANAGED_DB_HOST
    MANAGED_DB_PORT
    MANAGED_DB_NAME
    MANAGED_DB_USER
    MANAGED_DB_CREDENTIAL_REF
    CREDENTIALS_ENCRYPTION_KEY_ID
    INSTANCE_RESOURCE_ID
    DATA_DISK_RESOURCE_ID
    DATA_DISK_DEVICE_HINT
    ACCOUNT_OR_PROJECT
    ENVIRONMENT
    TERRAFORM_STATE_IDENTITY
    RDS_ENDPOINT
    RDS_SECRET_ARN
    RDS_DB_NAME
    MULTI_AGENT_ENABLED
    EMBEDDINGS_PROVIDER
    EMBEDDINGS_MODEL
    EMBEDDINGS_DIMENSIONS
    EMBEDDINGS_FALLBACK
    IMPLEMENT_WORK_ORDERS_REPO_URL
    IMPLEMENT_WORK_ORDERS_BASE_BRANCH
    IMPLEMENT_JIRA_TICKET_REPO_URL
    IMPLEMENT_JIRA_TICKET_BASE_BRANCH
    OPENCODE_VERSION
    TS_HARNESS_TOOLS_ROOT_HOST
    IDENTITY_PROVIDER
    OIDC_ISSUER
    OIDC_AUDIENCE
    OIDC_JWKS_URL
    OIDC_CLIENT_ID
    OIDC_AUTHORIZATION_ENDPOINT
    OIDC_TOKEN_ENDPOINT
    OIDC_PRESET
    OIDC_TOKEN_TYPE
    OIDC_GROUPS_CLAIM
    OIDC_ROLES_CLAIM
    OIDC_ALLOWED_DOMAINS
    OIDC_GROUPS_SOURCE
    IDENTITY_DIRECTORY
    DEPLOYMENT_PROFILE
    CODING_ECS_CLUSTER
    CODING_ECS_TASK_DEFINITION
    CODING_ECS_CONTAINER
    CODING_ECS_SUBNETS
    CODING_ECS_SECURITY_GROUPS
    CODING_EGRESS_PROXY_SERVICE
    CODING_EGRESS_DOMAINS_PARAMETER
    CODING_EGRESS_PROXY_URL
)

instance_env_path() {
    echo "${DATA_DIR:?DATA_DIR must be set}/.setup/instance-env.sh"
}

# Read a single `export KEY='value'` line; print value when non-empty.
_read_nonempty_export() {
    local file="$1" key="$2"
    local line raw value
    [[ -f "$file" ]] || return 1
    line="$(grep -E "^export ${key}=" "$file" 2>/dev/null | tail -n 1)" || true
    [[ -n "$line" ]] || return 1
    raw="${line#export ${key}=}"
    if [[ "$raw" == \'*\' && "$raw" == *\' ]]; then
        value="${raw:1:${#raw}-2}"
    else
        value="$raw"
    fi
    if is_blank_or_placeholder "$value"; then
        return 1
    fi
    printf '%s' "$value"
}

load_instance_env() {
    local path
    path="$(instance_env_path)"
    [[ -f "$path" ]] || return 0
    # shellcheck disable=SC1090
    source "$path"
}

# Persist Terraform/bootstrap vars so manual phase re-runs do not lose channel IDs.
write_instance_env() {
    local path key value existing
    path="$(instance_env_path)"
    mkdir -p "$(dirname "$path")"
    {
        for key in "${INSTANCE_ENV_KEYS[@]}"; do
            value="$(printenv "$key" 2>/dev/null || true)"
            if is_blank_or_placeholder "$value" && [[ -f "$path" ]]; then
                existing="$(_read_nonempty_export "$path" "$key" 2>/dev/null || true)"
                value="$existing"
            fi
            if ! is_blank_or_placeholder "$value"; then
                echo "export ${key}=$(printf '%q' "$value")"
            fi
        done
    } > "${path}.tmp"
    mv "${path}.tmp" "$path"
    chmod 600 "$path"
}

recover_slack_channels_from_runtime_config() {
    local config key channel
    config="${TS_HARNESS_CONFIG_DIR:-${DATA_DIR:-}/.ts-harness}/ts-harness-runtime.yaml"
    [[ -f "$config" ]] || return 0
    channel="$(grep -Eo 'channel:[A-Z0-9]+' "$config" 2>/dev/null | head -n 1 | cut -d: -f2- || true)"
    if is_blank_or_placeholder "$channel"; then
        channel="$(grep -Eo '\bC[0-9A-Z]{8,}\b' "$config" 2>/dev/null | head -n 1 || true)"
    fi
    if is_blank_or_placeholder "$channel"; then
        return 0
    fi
    for key in "${SLACK_CHANNEL_KEYS[@]}"; do
        if is_blank_or_placeholder "${!key:-}"; then
            export "$key=$channel"
        fi
    done
}

# Load Slack channel IDs from instance-env, prior compose-env, or rendered runtime config.
hydrate_slack_channel_env() {
    local compose_env_file="${1:-${COMPOSE_ENV:-${DATA_DIR:-}/.setup/compose-env.sh}}"
    local key existing
    load_instance_env
    for key in "${SLACK_CHANNEL_KEYS[@]}"; do
        if is_blank_or_placeholder "${!key:-}" && [[ -f "$compose_env_file" ]]; then
            existing="$(_read_nonempty_export "$compose_env_file" "$key" 2>/dev/null || true)"
            if ! is_blank_or_placeholder "$existing"; then
                export "$key=$existing"
            fi
        fi
    done
    recover_slack_channels_from_runtime_config
}

require_slack_channel_env() {
    local key phase_name="${1:-bootstrap}"
    for key in "${SLACK_CHANNEL_KEYS[@]}"; do
        if is_blank_or_placeholder "${!key:-}"; then
            echo "[phase:${phase_name}] ERROR: ${key} is required for Slack cron announcements." >&2
            echo "[phase:${phase_name}] Channel IDs are written to $(instance_env_path) by phase 10a from the runtime-config store." >&2
            echo "[phase:${phase_name}] Update terraform.tfvars, apply the runtime-config store, and run scripts/ops/sync-runtime-config.sh." >&2
            return 1
        fi
    done
}

source_required_env_files() {
    local env_file
    for env_file in "$@"; do
        if [[ ! -f "$env_file" ]]; then
            echo "ERROR: $env_file not found — prior phase did not complete" >&2
            exit 1
        fi
        # shellcheck disable=SC1090
        source "$env_file"
    done
    hydrate_slack_channel_env
}

reconstruct_compose_files() {
    COMPOSE_FILES=()
    while IFS= read -r token; do
        [[ -n "$token" ]] && COMPOSE_FILES+=("$token")
    done <<< "${COMPOSE_FILES_SERIALIZED:-}"

    if (( ${#COMPOSE_FILES[@]} == 0 )); then
        COMPOSE_FILES=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.memory-api.yml" -f "$ENTERPRISE_DOCKER_DIR/docker-compose.agent-runtime.yml")
        if is_managed_memory_backend; then
            COMPOSE_FILES+=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.managed-db.yml")
        else
            COMPOSE_FILES+=(-f "$ENTERPRISE_DOCKER_DIR/docker-compose.local-db.yml")
        fi
    fi
}

# Update managed DB password exports in compose-env without clobbering other keys.
sync_compose_db_password() {
    local compose_env_file="${1:-${COMPOSE_ENV:-${DATA_DIR:-}/.setup/compose-env.sh}}"
    local password="${2:-${TS_HARNESS_DB_PASSWORD:-}}"

    if [[ -z "${password//[[:space:]]/}" ]]; then
        echo "ERROR: sync_compose_db_password requires TS_HARNESS_DB_PASSWORD" >&2
        return 1
    fi
    if [[ ! -f "$compose_env_file" ]]; then
        echo "ERROR: $compose_env_file not found — run phase 12-compose-up before syncing DB password" >&2
        return 1
    fi

    python3 - "$compose_env_file" "$password" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
password = sys.argv[2]
text = path.read_text()
for key in ("TS_HARNESS_DB_PASSWORD",):
    pattern = rf"^export {key}=.*$"
    replacement = f"export {key}={password!r}"
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count == 0:
        text = text.rstrip() + f"\nexport {key}={password!r}\n"
path.write_text(text)
PY
    chmod 600 "$compose_env_file"
}

# Postgres URL for runtime sessions, transcripts, and Google OAuth tokens, built
# from TS_HARNESS_DB_* so password rotation carries through. An explicit
# TS_HARNESS_SESSION_DB_URL wins. Prints nothing (JSONL session store) when the
# DB identity is incomplete.
session_db_url() {
    if ! is_blank_or_placeholder "${TS_HARNESS_SESSION_DB_URL:-}"; then
        printf '%s' "$TS_HARNESS_SESSION_DB_URL"
        return 0
    fi
    local key
    for key in TS_HARNESS_DB_HOST TS_HARNESS_DB_USER TS_HARNESS_DB_NAME TS_HARNESS_DB_PASSWORD; do
        if is_blank_or_placeholder "${!key:-}"; then
            return 0
        fi
    done
    local sslmode="${PGSSLMODE:-}"
    if [[ -z "$sslmode" ]]; then
        if is_managed_memory_backend; then
            sslmode=require
        else
            sslmode=disable
        fi
    fi
    DB_USER="$TS_HARNESS_DB_USER" DB_PASSWORD="$TS_HARNESS_DB_PASSWORD" \
        DB_HOST="$TS_HARNESS_DB_HOST" DB_PORT="${TS_HARNESS_DB_PORT:-5432}" \
        DB_NAME="$TS_HARNESS_DB_NAME" DB_SSLMODE="$sslmode" \
        python3 -c '
import os
from urllib.parse import quote

e = os.environ
print(
    "postgresql://{}:{}@{}:{}/{}?sslmode={}".format(
        quote(e["DB_USER"], safe=""),
        quote(e["DB_PASSWORD"], safe=""),
        e["DB_HOST"],
        e["DB_PORT"],
        quote(e["DB_NAME"], safe=""),
        quote(e["DB_SSLMODE"], safe=""),
    ),
    end="",
)
'
}

# Docker Compose interpolates all services (including inactive profiles) from the shell env.
run_docker_compose_as_admin() {
    sudo -u "$ADMIN_USER" \
        env TS_HARNESS_DB_PASSWORD="${TS_HARNESS_DB_PASSWORD:-}" \
            TS_HARNESS_DB_HOST="${TS_HARNESS_DB_HOST:-}" \
            TS_HARNESS_DB_PORT="${TS_HARNESS_DB_PORT:-5432}" \
            TS_HARNESS_DB_USER="${TS_HARNESS_DB_USER:-}" \
            TS_HARNESS_DB_NAME="${TS_HARNESS_DB_NAME:-}" \
            TS_HARNESS_SESSION_DB_URL="$(session_db_url)" \
            ENTERPRISE_ASSETS_DIR="${ENTERPRISE_ASSETS_DIR:-}" \
            TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}" \
            TS_HARNESS_TOOLS_ROOT_HOST="${TS_HARNESS_TOOLS_ROOT_HOST:-${DATA_DIR}/.ts-harness/tools}" \
            TS_HARNESS_TOOLS_ROOT="${TS_HARNESS_TOOLS_ROOT:-/tools}" \
            CLOUD_REGION="${CLOUD_REGION:-${AWS_REGION:-}}" \
            AWS_REGION="${CLOUD_REGION:-${AWS_REGION:-}}" \
            EMBEDDINGS_PROVIDER="${EMBEDDINGS_PROVIDER:-openai}" \
            EMBEDDINGS_MODEL="${EMBEDDINGS_MODEL:-text-embedding-3-small}" \
            EMBEDDINGS_DIMENSIONS="${EMBEDDINGS_DIMENSIONS:-1536}" \
            OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
            VOYAGE_API_KEY="${VOYAGE_API_KEY:-}" \
            GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
            MISTRAL_API_KEY="${MISTRAL_API_KEY:-}" \
            GATEWAY_TOKEN="${GATEWAY_TOKEN:-}" \
            MEMORY_API_TOKEN="${MEMORY_API_TOKEN:-}" \
            SANDBOX_WORKER_TOKEN="${SANDBOX_WORKER_TOKEN:-}" \
            CODING_JOB_SECRET="${CODING_JOB_SECRET:-}" \
            SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}" \
            SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}" \
            SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET:-}" \
            GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
            WORKFLOW_RUNNER_ENABLED="${WORKFLOW_RUNNER_ENABLED:-false}" \
            WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS="${WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS:-10}" \
            WORKFLOW_RUNNER_BATCH_SIZE="${WORKFLOW_RUNNER_BATCH_SIZE:-5}" \
            WORKFLOW_RUNNER_LEASE_SECONDS="${WORKFLOW_RUNNER_LEASE_SECONDS:-1800}" \
            WORKFLOW_RUNNER_STALE_POLICY="${WORKFLOW_RUNNER_STALE_POLICY:-block}" \
            WORKFLOW_RUNNER_ADAPTER="${WORKFLOW_RUNNER_ADAPTER:-gateway-http}" \
            CODING_JOBS_ENABLED="${CODING_JOBS_ENABLED:-false}" \
            CODING_MAX_RUNNING="${CODING_MAX_RUNNING:-3}" \
            CODING_LAUNCHER="${CODING_LAUNCHER:-}" \
            DEPLOYMENT_PROFILE="${DEPLOYMENT_PROFILE:-single_host}" \
            CODING_ECS_CLUSTER="${CODING_ECS_CLUSTER:-}" \
            CODING_ECS_TASK_DEFINITION="${CODING_ECS_TASK_DEFINITION:-}" \
            CODING_ECS_CONTAINER="${CODING_ECS_CONTAINER:-coding-worker}" \
            CODING_ECS_SUBNETS="${CODING_ECS_SUBNETS:-}" \
            CODING_ECS_SECURITY_GROUPS="${CODING_ECS_SECURITY_GROUPS:-}" \
            AGENT_RUNTIME_URL="${AGENT_RUNTIME_URL:-http://127.0.0.1:${GATEWAY_PORT:-18789}}" \
            RUNTIME_FAKE_PROVIDER="${RUNTIME_FAKE_PROVIDER:-false}" \
        docker compose "${COMPOSE_FILES[@]}" "$@"
}

restart_agent_runtime() {
    run_docker_compose_as_admin restart agent-runtime
}

persist_env_key() {
    local env_file="$1" key="$2" value="$3"

    if [[ -z "${value//[[:space:]]/}" ]]; then
        return 0
    fi

    mkdir -p "$(dirname "$env_file")"
    touch "$env_file"
    grep -q "^${key}=" "$env_file" 2>/dev/null && sed -i "/^${key}=/d" "$env_file"
    write_dotenv_var "$key" "$value" >> "$env_file"
    chmod 600 "$env_file"
}

restart_ts_harness_services() {
    local services=("$@")
    run_docker_compose_as_admin restart "${services[@]}"
}

recreate_ts_harness_services() {
    local services=("$@")
    run_docker_compose_as_admin up -d --force-recreate "${services[@]}"
}

wait_for_agent_runtime_health() {
    local phase="${1:-unknown}"
    local timeout="${AGENT_RUNTIME_HEALTH_TIMEOUT:-120}"
    local interval="${AGENT_RUNTIME_HEALTH_INTERVAL:-3}"
    local elapsed=0
    local health_url="http://127.0.0.1:${GATEWAY_PORT:-18789}/healthz"

    echo "[phase:${phase}] Waiting for agent-runtime health (timeout ${timeout}s)..."
    while (( elapsed < timeout )); do
        if run_docker_compose_as_admin exec -T \
            -e AGENT_RUNTIME_HEALTH_URL="$health_url" \
            agent-runtime python3 -c "
import json
import os
import sys
import urllib.request

url = os.environ.get('AGENT_RUNTIME_HEALTH_URL', 'http://127.0.0.1:18789/healthz')
try:
    payload = json.load(urllib.request.urlopen(url, timeout=5))
except Exception:
    sys.exit(1)
components = payload.get('components') or {}
required = ('provider', 'memory_api', 'scheduler')
ready = payload.get('status') == 'ok' and all(
    components.get(name, {}).get('status') == 'ok' for name in required
)
sys.exit(0 if ready else 1)
" 2>/dev/null; then
            echo "[phase:${phase}] agent-runtime health OK."
            return 0
        fi
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done

    echo "[phase:${phase}] ERROR: agent-runtime did not become healthy within ${timeout}s." >&2
    return 1
}
