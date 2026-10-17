#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

PHASE_NAME="11e-preflight-embeddings"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)

if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        echo "[phase:${PHASE_NAME}] Already validated for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
if [[ ! -f "$SECRETS_ENV" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $SECRETS_ENV not found — run 10-fetch-secrets.sh first" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$SECRETS_ENV"

EMBED_SH="$ENTERPRISE_ASSETS_DIR/scripts/embed.sh"
CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"

ACTIVE_PROVIDER="${EMBEDDINGS_PROVIDER:-openai}"
: "${CLOUD_PROVIDER:=aws}"
if [[ -z "${ACTIVE_PROVIDER//[[:space:]]/}" || "$ACTIVE_PROVIDER" == *__TF_*__* ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: EMBEDDINGS_PROVIDER is not set. Configure embeddings_provider in Terraform." >&2
    exit 1
fi
echo "[phase:${PHASE_NAME}] Active embedding provider: $ACTIVE_PROVIDER (cloud=${CLOUD_PROVIDER})"

# Provider compatibility:
# - openai/voyage/gemini/mistral: allowed on aws and gcp
# - bedrock: aws only
# - local: allowed here for single-agent paths; memory-api multi-agent still blocks local
case "$ACTIVE_PROVIDER" in
    openai|voyage|gemini|mistral) ;;
    bedrock)
        if [[ "$CLOUD_PROVIDER" != "aws" ]]; then
            echo "[phase:${PHASE_NAME}] ERROR: bedrock embeddings require CLOUD_PROVIDER=aws (got ${CLOUD_PROVIDER})." >&2
            echo "[phase:${PHASE_NAME}] On GCP use openai, voyage, gemini, or mistral." >&2
            exit 1
        fi
        ;;
    local) ;;
    *)
        echo "[phase:${PHASE_NAME}] ERROR: Unknown provider: $ACTIVE_PROVIDER" >&2
        exit 1
        ;;
esac

# Check: matching API key present (hard fail for cloud providers)
case "$ACTIVE_PROVIDER" in
    openai)  KEY_NAME="OPENAI_API_KEY";  KEY_VAL="${OPENAI_API_KEY:-}"  ;;
    voyage)  KEY_NAME="VOYAGE_API_KEY";  KEY_VAL="${VOYAGE_API_KEY:-}"  ;;
    gemini)  KEY_NAME="GEMINI_API_KEY";  KEY_VAL="${GEMINI_API_KEY:-}"  ;;
    mistral) KEY_NAME="MISTRAL_API_KEY"; KEY_VAL="${MISTRAL_API_KEY:-}" ;;
    bedrock)
        KEY_NAME="AWS_STS"
        if aws sts get-caller-identity &>/dev/null; then
            KEY_VAL="ok"
            echo "[phase:${PHASE_NAME}] AWS credentials valid for bedrock provider."
        else
            echo "[phase:${PHASE_NAME}] ERROR: bedrock provider selected but AWS credentials unavailable." >&2
            exit 1
        fi
        ;;
    local)
        KEY_NAME="local"; KEY_VAL="ok"
        echo "[phase:${PHASE_NAME}] Local provider selected; no API key required."
        ;;
esac

if [[ "$KEY_NAME" != "AWS_STS" && "$KEY_NAME" != "local" ]]; then
    if [[ -z "${KEY_VAL//[[:space:]]/}" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: Provider '$ACTIVE_PROVIDER' requires $KEY_NAME but it is not set." >&2
        echo "[phase:${PHASE_NAME}] Populate the secret in the cloud secret store and re-run phase 10-fetch-secrets." >&2
        exit 1
    fi
    echo "[phase:${PHASE_NAME}] $KEY_NAME: present."
fi

# Multi-key warning (warn only, do not fail)
PROVIDER_KEYS=(OPENAI_API_KEY VOYAGE_API_KEY GEMINI_API_KEY MISTRAL_API_KEY)
FOUND_KEYS=()
for pk in "${PROVIDER_KEYS[@]}"; do
    [[ -n "${!pk:-}" ]] && FOUND_KEYS+=("$pk")
done
if (( ${#FOUND_KEYS[@]} > 1 )); then
    echo "[phase:${PHASE_NAME}][warn] Multiple provider keys present: ${FOUND_KEYS[*]}. Active provider is '$ACTIVE_PROVIDER'; other keys are ignored."
fi

# Delegate dimension scan to existing check script
DIM_CHECK="$ENTERPRISE_ASSETS_DIR/scripts/check-memory-embedding-dimension.sh"
if [[ -f "$DIM_CHECK" ]]; then
    chmod +x "$DIM_CHECK"
    TS_HARNESS_CONFIG_DIR="$CONFIG_DIR" \
    TS_HARNESS_WORKSPACE_DIR="$CONFIG_DIR" \
    ENTERPRISE_MEMORY_EMBEDDING_DIM="${EMBEDDINGS_DIMENSIONS:-1536}" \
    ENTERPRISE_EMBEDDING_CHECK_STRICT=0 \
        "$DIM_CHECK" 2>&1 | sed "s|^|[phase:${PHASE_NAME}]   |" \
    || echo "[phase:${PHASE_NAME}] WARNING: Dimension check reported issues (non-fatal at preflight)."
fi

# Live round-trip embedding test
if [[ -f "$EMBED_SH" ]]; then
    chmod +x "$EMBED_SH"
    echo "[phase:${PHASE_NAME}] Running live embedding round-trip test..."
    EMBED_RESULT=$(
        EMBEDDINGS_PROVIDER="$ACTIVE_PROVIDER" \
        EMBEDDINGS_MODEL="${EMBEDDINGS_MODEL:-text-embedding-3-small}" \
        EMBEDDINGS_DIMENSIONS="${EMBEDDINGS_DIMENSIONS:-1536}" \
        CLOUD_PROVIDER="${CLOUD_PROVIDER:-aws}" \
        OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
        VOYAGE_API_KEY="${VOYAGE_API_KEY:-}" \
        GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
        MISTRAL_API_KEY="${MISTRAL_API_KEY:-}" \
        "$EMBED_SH" "healthcheck" 2>&1
    ) || {
        echo "[phase:${PHASE_NAME}] ERROR: scripts/embed.sh healthcheck failed:" >&2
        echo "$EMBED_RESULT" >&2
        exit 1
    }
    echo "[phase:${PHASE_NAME}] Embedding round-trip: OK"
else
    echo "[phase:${PHASE_NAME}] WARNING: scripts/embed.sh not found; skipping live round-trip test."
fi

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
