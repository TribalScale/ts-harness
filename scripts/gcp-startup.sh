#!/bin/bash
set -euo pipefail

# Values below are substituted at terraform apply time (see infra/gcp/compute.tf).
ADMIN_USER="__TF_ADMIN_USER__"
AGENT_RUNTIME_VERSION="__TF_AGENT_RUNTIME_VERSION__"
GCP_PROJECT_ID="__TF_GCP_PROJECT_ID__"
CLOUD_PROVIDER="__TF_CLOUD_PROVIDER__"
CLOUD_REGION="__TF_CLOUD_REGION__"
CLOUD_ZONE="__TF_CLOUD_ZONE__"
SECRETS_PREFIX="__TF_SECRETS_PREFIX__"
ENTERPRISE_REPO_URL="__TF_ENTERPRISE_REPO__"
MEMORY_BACKEND="__TF_MEMORY_BACKEND__"
MANAGED_DB_HOST="__TF_MANAGED_DB_HOST__"
MANAGED_DB_PORT="__TF_MANAGED_DB_PORT__"
MANAGED_DB_NAME="__TF_MANAGED_DB_NAME__"
MANAGED_DB_USER="__TF_MANAGED_DB_USER__"
MANAGED_DB_CREDENTIAL_REF="__TF_MANAGED_DB_CREDENTIAL_REF__"
CREDENTIALS_ENCRYPTION_KEY_ID="__TF_CREDENTIALS_ENCRYPTION_KEY_ID__"
INSTANCE_RESOURCE_ID="__TF_INSTANCE_RESOURCE_ID__"
DATA_DISK_DEVICE_HINT="__TF_DATA_DISK_DEVICE_HINT__"

# =========================================================================
# Derived paths (all computed from the Terraform-substituted vars above)
# =========================================================================
LOG_FILE="/var/log/ts-harness-setup.log"
COMPLETION_MARKER="/vol/ts-harness-data/.setup-complete"
DATA_DIR="/vol/ts-harness-data"
TS_HARNESS_CONFIG_DIR="$DATA_DIR/.ts-harness"
TS_HARNESS_WORKSPACE_DIR="$TS_HARNESS_CONFIG_DIR/workspace"
ADMIN_HOME="/home/$ADMIN_USER"
ENTERPRISE_ASSETS_DIR="$ADMIN_HOME/enterprise-assets"
ENTERPRISE_DOCKER_DIR="$ENTERPRISE_ASSETS_DIR/docker"
SCRIPT_DIR="$ENTERPRISE_ASSETS_DIR/scripts"

# Legacy AWS aliases left empty on GCP (resolve_cloud_neutral_env backfills from neutral).
AWS_REGION="$CLOUD_REGION"
RDS_ENDPOINT="$MANAGED_DB_HOST"
RDS_SECRET_ARN="$MANAGED_DB_CREDENTIAL_REF"
RDS_DB_NAME="$MANAGED_DB_NAME"

# =========================================================================
# Export all vars for phase scripts
# =========================================================================
export ADMIN_USER AGENT_RUNTIME_VERSION GCP_PROJECT_ID CLOUD_PROVIDER CLOUD_REGION CLOUD_ZONE
export AWS_REGION SECRETS_PREFIX ENTERPRISE_REPO_URL
export MEMORY_BACKEND RDS_ENDPOINT RDS_SECRET_ARN RDS_DB_NAME
export MANAGED_DB_HOST MANAGED_DB_PORT MANAGED_DB_NAME MANAGED_DB_USER MANAGED_DB_CREDENTIAL_REF
export CREDENTIALS_ENCRYPTION_KEY_ID INSTANCE_RESOURCE_ID DATA_DISK_DEVICE_HINT
export LOG_FILE COMPLETION_MARKER DATA_DIR TS_HARNESS_CONFIG_DIR TS_HARNESS_WORKSPACE_DIR
export ADMIN_HOME ENTERPRISE_ASSETS_DIR ENTERPRISE_DOCKER_DIR SCRIPT_DIR
export ENTERPRISE_ASSETS_COMMIT="${ENTERPRISE_ASSETS_COMMIT:-unknown}"
export ENTERPRISE_MEMORY_EMBEDDING_DIM="${ENTERPRISE_MEMORY_EMBEDDING_DIM:-1536}"
export ENTERPRISE_EMBEDDING_CHECK_STRICT="${ENTERPRISE_EMBEDDING_CHECK_STRICT:-0}"
export GATEWAY_PORT="18789"
export TS_HARNESS_HOME_VOLUME="$DATA_DIR"
export AGENT_RUNTIME_URL="${AGENT_RUNTIME_URL:-http://127.0.0.1:18789}"

# =========================================================================
# Logging
# =========================================================================
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=========================================="
echo "TS Harness Setup (GCP) — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=========================================="

# =========================================================================
# Backward-compatible completion marker check
# =========================================================================
if [[ -f "$COMPLETION_MARKER" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$COMPLETION_MARKER" 2>/dev/null | cut -d= -f2 || true)"
    INSTALLED_ENTERPRISE_ASSETS_COMMIT="$(grep '^enterprise_assets_commit=' "$COMPLETION_MARKER" 2>/dev/null | cut -d= -f2 || true)"
    CURRENT_ENTERPRISE_ASSETS_COMMIT="$(git -C "$ENTERPRISE_ASSETS_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "$AGENT_RUNTIME_VERSION" && "$INSTALLED_ENTERPRISE_ASSETS_COMMIT" == "$CURRENT_ENTERPRISE_ASSETS_COMMIT" ]]; then
        echo "Setup already completed for $AGENT_RUNTIME_VERSION and enterprise assets $CURRENT_ENTERPRISE_ASSETS_COMMIT (marker at $COMPLETION_MARKER). Exiting."
        exit 0
    else
        echo "Re-running setup (installed: ${INSTALLED_VERSION:-unknown}, desired: $AGENT_RUNTIME_VERSION)."
    fi
fi

# =========================================================================
# Bootstrap: ensure git + gcloud + enterprise repo available
# =========================================================================
if ! command -v git &>/dev/null; then
    echo "Bootstrap: installing git for enterprise repo clone..."
    apt-get update -y -qq
    apt-get install -y -qq git
fi

# Ubuntu images often ship without gcloud; install Google Cloud CLI if missing.
if ! command -v gcloud &>/dev/null; then
    echo "Bootstrap: installing Google Cloud CLI for Secret Manager / metadata access..."
    apt-get update -y -qq
    apt-get install -y -qq apt-transport-https ca-certificates gnupg curl
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list
    apt-get update -y -qq
    apt-get install -y -qq google-cloud-cli
fi

# Prefer metadata server for instance identity when TF left it blank.
if [[ -z "${INSTANCE_RESOURCE_ID}" ]]; then
    INSTANCE_RESOURCE_ID="$(curl -s -H 'Metadata-Flavor: Google' \
        http://metadata.google.internal/computeMetadata/v1/instance/id 2>/dev/null || true)"
    export INSTANCE_RESOURCE_ID
fi

mkdir -p "$ADMIN_HOME"
if [[ ! -d "$ENTERPRISE_ASSETS_DIR/.git" ]]; then
    echo "Bootstrap: cloning enterprise repo to obtain phase scripts..."
    # Secret Manager IDs use prefix-key (no '/'); logical catalog remains prefix/key.
    BOOT_GH_TOKEN="$(gcloud secrets versions access latest \
        --secret="${SECRETS_PREFIX}-github-token" \
        --project="${GCP_PROJECT_ID}" 2>/dev/null | tr -d '[:space:]' || true)"
    GIT_CLONE_ARGS=()
    if [[ -n "$BOOT_GH_TOKEN" && ! "$BOOT_GH_TOKEN" =~ ^[[:space:]]*$ && "$ENTERPRISE_REPO_URL" == https://github.com/* ]]; then
        BOOT_GH_BASIC_AUTH="$(printf 'x-access-token:%s' "$BOOT_GH_TOKEN" | base64 | tr -d '\n')"
        GIT_CLONE_ARGS=(-c "http.https://github.com/.extraheader=Authorization: Basic ${BOOT_GH_BASIC_AUTH}")
    fi
    git "${GIT_CLONE_ARGS[@]}" clone "$ENTERPRISE_REPO_URL" "$ENTERPRISE_ASSETS_DIR"
    git -C "$ENTERPRISE_ASSETS_DIR" remote set-url origin "$ENTERPRISE_REPO_URL"
    unset BOOT_GH_TOKEN BOOT_GH_BASIC_AUTH GIT_CLONE_ARGS
fi
ENTERPRISE_ASSETS_COMMIT="$(git -C "$ENTERPRISE_ASSETS_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
export ENTERPRISE_ASSETS_COMMIT

PHASES_DIR="$SCRIPT_DIR/phases"
if [[ ! -d "$PHASES_DIR" ]]; then
    echo "ERROR: Enterprise repo does not contain scripts/phases/ (expected $PHASES_DIR)" >&2
    exit 1
fi

# Source shared utility functions from the single canonical location and export
# them so child bash subprocesses (phase scripts) inherit them via export -f.
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"
export -f fetch_secret is_blank_or_placeholder require_env write_env_var write_dotenv_var
export -f enterprise_assets_commit calculate_phase_fingerprint phase_marker_value
export -f phase_marker_matches write_phase_marker resolve_ebs_data_device
export -f resolve_cloud_neutral_env is_managed_memory_backend
resolve_cloud_neutral_env

BOOTSTRAP_RUN="$SCRIPT_DIR/bootstrap/run.sh"
if [[ ! -f "$BOOTSTRAP_RUN" ]]; then
    echo "ERROR: Shared bootstrap runner not found: $BOOTSTRAP_RUN" >&2
    exit 1
fi
chmod +x "$BOOTSTRAP_RUN" "$SCRIPT_DIR/bootstrap/phases.sh" 2>/dev/null || true

echo "------------------------------------------"
echo ">>> Delegating phase loop to scripts/bootstrap/run.sh"
# shellcheck disable=SC1091
bash "$BOOTSTRAP_RUN"

echo "=========================================="
echo "TS Harness setup finished successfully."
echo "=========================================="
