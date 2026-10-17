#!/bin/bash
set -euo pipefail

PHASE_NAME="00-init"
# DATA_DIR is not yet mounted at this phase — no done-marker; always runs (lightweight).

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

echo "[phase:${PHASE_NAME}] Validating environment substitutions..."

# Prefer cloud-neutral names; accept legacy AWS_REGION until providers fully migrate.
: "${CLOUD_PROVIDER:=aws}"
if [[ -z "${CLOUD_REGION:-}" && -n "${AWS_REGION:-}" ]]; then
    CLOUD_REGION="$AWS_REGION"
fi
if [[ -z "${AWS_REGION:-}" && -n "${CLOUD_REGION:-}" ]]; then
    AWS_REGION="$CLOUD_REGION"
fi
export CLOUD_PROVIDER CLOUD_REGION AWS_REGION

# Guard: fail fast if Terraform did not substitute all required tokens.
REQUIRED_VARS=(
    ADMIN_USER
    AGENT_RUNTIME_VERSION
    SECRETS_PREFIX
    ENTERPRISE_REPO_URL
    MEMORY_BACKEND
)

for var in "${REQUIRED_VARS[@]}"; do
    val="${!var:-}"
    if [[ -z "$val" || "$val" == *__TF_*__* ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: Required variable ${var} is unset or contains an unsubstituted Terraform token: '${val}'" >&2
        exit 1
    fi
done

if [[ -z "${CLOUD_REGION:-}" || "${CLOUD_REGION}" == *__TF_*__* ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: CLOUD_REGION/AWS_REGION is unset or contains an unsubstituted Terraform token: '${CLOUD_REGION:-}'" >&2
    exit 1
fi

echo "[phase:${PHASE_NAME}] All required variables are set."
echo "[phase:${PHASE_NAME}] CLOUD_PROVIDER=${CLOUD_PROVIDER}"
echo "[phase:${PHASE_NAME}] CLOUD_REGION=${CLOUD_REGION}"
echo "[phase:${PHASE_NAME}] AGENT_RUNTIME_VERSION=${AGENT_RUNTIME_VERSION}"
echo "[phase:${PHASE_NAME}] MEMORY_BACKEND=${MEMORY_BACKEND}"
echo "[phase:${PHASE_NAME}] ADMIN_USER=${ADMIN_USER}"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
