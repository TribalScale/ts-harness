#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"


PHASE_NAME="10-fetch-secrets"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"


echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

echo "[phase:${PHASE_NAME}] Fetching secrets (prefix: $SECRETS_PREFIX, provider=${CLOUD_PROVIDER:-aws})..."

resolve_cloud_neutral_env
ensure_cloud_adapter

ANTHROPIC_API_KEY="$(fetch_secret "anthropic-api-key" false)"
GATEWAY_TOKEN="$(fetch_secret "gateway-token" false)"
MEMORY_API_TOKEN="$(fetch_secret "memory-api-token" false)"
SANDBOX_WORKER_TOKEN="$(fetch_secret "sandbox-worker-token" false)"
if is_managed_memory_backend; then
    if [[ -z "${MANAGED_DB_CREDENTIAL_REF:-}" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: memory_backend=${MEMORY_BACKEND} but MANAGED_DB_CREDENTIAL_REF (or RDS_SECRET_ARN) is empty" >&2
        exit 1
    fi
    echo "[phase:${PHASE_NAME}] Managed DB backend: loading password from credential ref (not ${SECRETS_PREFIX}/db-password)."
    MANAGED_DB_JSON="$(cloud_fetch_managed_db_credentials "$MANAGED_DB_CREDENTIAL_REF")"
    TS_HARNESS_DB_PASSWORD="$(echo "$MANAGED_DB_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
    MANAGED_DB_PASSWORD="$TS_HARNESS_DB_PASSWORD"
else
    TS_HARNESS_DB_PASSWORD="$(fetch_secret "db-password" true)"
fi
SLACK_BOT_TOKEN="$(fetch_secret "slack-bot-token" true)"
SLACK_APP_TOKEN="$(fetch_secret "slack-app-token" true)"
SLACK_ALLOWED_IDS="$(fetch_secret "slack-allowed-ids" true)"
SLACK_SIGNING_SECRET="$(fetch_secret "slack-signing-secret" true)"

# Fetch the active embedding provider's API key.
# bedrock and local use credential chains and don't need a stored key.
OPENAI_API_KEY=""
VOYAGE_API_KEY=""
GEMINI_API_KEY=""
MISTRAL_API_KEY=""
case "${EMBEDDINGS_PROVIDER:-openai}" in
    openai)  OPENAI_API_KEY="$(fetch_secret "openai-api-key" true)" ;;
    voyage)  VOYAGE_API_KEY="$(fetch_secret "voyage-api-key" true)" ;;
    gemini)  GEMINI_API_KEY="$(fetch_secret "gemini-api-key" true)" ;;
    mistral) MISTRAL_API_KEY="$(fetch_secret "mistral-api-key" true)" ;;
    bedrock|local)
        echo "[phase:${PHASE_NAME}] Embedding provider '${EMBEDDINGS_PROVIDER}' uses credential chain; no stored key fetched."
        ;;
    *)
        echo "[phase:${PHASE_NAME}] WARNING: Unknown EMBEDDINGS_PROVIDER '${EMBEDDINGS_PROVIDER}'; no embedding key fetched."
        ;;
esac

GOOGLE_WORKSPACE_SA_JSON="$(fetch_secret "google-workspace-sa-json" true 2>/dev/null || true)"
GOOGLE_OAUTH_CLIENT_ID="$(fetch_secret "google-oauth-client-id" true 2>/dev/null || true)"
GOOGLE_OAUTH_CLIENT_SECRET="$(fetch_secret "google-oauth-client-secret" true 2>/dev/null || true)"
GOOGLE_OAUTH_CALLBACK_URL="$(fetch_secret "google-oauth-callback-url" true 2>/dev/null || true)"
AGENT_CREDENTIALS_KMS_KEY_ID="$(fetch_secret "agent-credentials-kms-key-id" true 2>/dev/null || true)"
CREDENTIALS_ENCRYPTION_KEY_ID="${CREDENTIALS_ENCRYPTION_KEY_ID:-$AGENT_CREDENTIALS_KMS_KEY_ID}"
AGENT_CREDENTIALS_KMS_KEY_ID="${AGENT_CREDENTIALS_KMS_KEY_ID:-$CREDENTIALS_ENCRYPTION_KEY_ID}"
GOOGLE_WORKSPACE_DEFAULT_EMAIL="$(fetch_secret "google-workspace-default-email" true 2>/dev/null || true)"
GOOGLE_COMMS_EMAIL="$(fetch_secret "google-comms-email" true 2>/dev/null || true)"
GOOGLE_MARKETING_EMAIL="$(fetch_secret "google-marketing-email" true 2>/dev/null || true)"
GOOGLE_SALES_EMAIL="$(fetch_secret "google-sales-email" true 2>/dev/null || true)"
GOOGLE_PM_EMAIL="$(fetch_secret "google-pm-email" true 2>/dev/null || true)"
GOOGLE_PM_PRD_FOLDER_ID="$(fetch_secret "google-pm-prd-folder-id" true 2>/dev/null || true)"
GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID="$(fetch_secret "google-delivery-artifacts-folder-id" true 2>/dev/null || true)"
CODING_BACKEND_API_KEY="$(fetch_secret "coding-backend-api-key" true 2>/dev/null || true)"
CODING_BACKEND_PROVIDER="$(fetch_secret "coding-backend-provider" true 2>/dev/null || true)"
CODING_BACKEND_MODEL="$(fetch_secret "coding-backend-model" true 2>/dev/null || true)"
GITHUB_TOKEN="$(fetch_secret "github-token" true 2>/dev/null || true)"
CODING_JOB_SECRET="$(fetch_secret "coding-job-secret" true 2>/dev/null || true)"
GITHUB_APP_WEBHOOK_SECRET="$(fetch_secret "github-app-webhook-secret" true 2>/dev/null || true)"
if is_blank_or_placeholder "$GITHUB_APP_WEBHOOK_SECRET"; then GITHUB_APP_WEBHOOK_SECRET=""; fi
# The runtime turns literal \n back into newlines, so the PEM fits on one .env line.
GITHUB_APP_PRIVATE_KEY="$(fetch_secret "github-app-private-key" true 2>/dev/null || true)"
if is_blank_or_placeholder "$GITHUB_APP_PRIVATE_KEY"; then
    GITHUB_APP_PRIVATE_KEY=""
else
    GITHUB_APP_PRIVATE_KEY="$(printf '%s' "$GITHUB_APP_PRIVATE_KEY" | awk 'BEGIN{ORS="\\n"} {print}')"
fi
if is_blank_or_placeholder "$CODING_JOB_SECRET"; then CODING_JOB_SECRET=""; fi
LLM_PROXY_SECRET="$(fetch_secret "llm-proxy-secret" true 2>/dev/null || true)"
if is_blank_or_placeholder "$LLM_PROXY_SECRET"; then LLM_PROXY_SECRET=""; fi

# Jira: a service account OAuth credential (client id/secret) or an email and API token.
ATLASSIAN_SITE_URL="$(fetch_optional_secret "jira-url")"
ATLASSIAN_EMAIL="$(fetch_optional_secret "jira-username")"
ATLASSIAN_API_TOKEN="$(fetch_optional_secret "jira-token")"
ATLASSIAN_CLIENT_ID="$(fetch_optional_secret "jira-client-id")"
ATLASSIAN_CLIENT_SECRET="$(fetch_optional_secret "jira-client-secret")"
ATLASSIAN_CLOUD_ID="$(fetch_optional_secret "jira-cloud-id")"
for jira_var in ATLASSIAN_SITE_URL ATLASSIAN_EMAIL ATLASSIAN_API_TOKEN ATLASSIAN_CLIENT_ID ATLASSIAN_CLIENT_SECRET ATLASSIAN_CLOUD_ID; do
    if is_blank_or_placeholder "${!jira_var}"; then printf -v "$jira_var" '%s' ""; fi
done
if [[ -n "$ATLASSIAN_CLIENT_ID" && -n "$ATLASSIAN_CLIENT_SECRET" ]]; then
    echo "[phase:${PHASE_NAME}] Jira auth: service account (oauth)"
elif [[ -n "$ATLASSIAN_SITE_URL" && -n "$ATLASSIAN_EMAIL" && -n "$ATLASSIAN_API_TOKEN" ]]; then
    echo "[phase:${PHASE_NAME}] Jira auth: email and API token (basic)"
else
    echo "[phase:${PHASE_NAME}] WARNING: Jira credentials missing; atlassian_search and Jira compensation will be unavailable." >&2
fi

# Terraform seeds secrets as a single space; treat that as unset.
if is_blank_or_placeholder "$CODING_BACKEND_API_KEY"; then CODING_BACKEND_API_KEY=""; fi
if is_blank_or_placeholder "$CODING_BACKEND_PROVIDER"; then CODING_BACKEND_PROVIDER=""; fi
if is_blank_or_placeholder "$CODING_BACKEND_MODEL"; then CODING_BACKEND_MODEL=""; fi
if is_blank_or_placeholder "$GITHUB_TOKEN"; then GITHUB_TOKEN=""; fi

GOOGLE_WORKSPACE_SA_JSON_PATH=""
if [[ -n "${GOOGLE_WORKSPACE_SA_JSON//[[:space:]]/}" ]]; then
    GOOGLE_WORKSPACE_SA_JSON_PATH="$DATA_DIR/.setup/google-workspace-sa.json"
    printf '%s' "$GOOGLE_WORKSPACE_SA_JSON" > "$GOOGLE_WORKSPACE_SA_JSON_PATH"
    chmod 600 "$GOOGLE_WORKSPACE_SA_JSON_PATH"
    echo "[phase:${PHASE_NAME}] Wrote service account JSON to $GOOGLE_WORKSPACE_SA_JSON_PATH"
fi

# Trim whitespace from password.
TS_HARNESS_DB_PASSWORD="${TS_HARNESS_DB_PASSWORD#"${TS_HARNESS_DB_PASSWORD%%[![:space:]]*}"}"
TS_HARNESS_DB_PASSWORD="${TS_HARNESS_DB_PASSWORD%"${TS_HARNESS_DB_PASSWORD##*[![:space:]]}"}"

if is_blank_or_placeholder "$ANTHROPIC_API_KEY"; then
    echo "[phase:${PHASE_NAME}] ERROR: Failed to retrieve Anthropic API key (${SECRETS_PREFIX}/anthropic-api-key)" >&2
    exit 1
fi
if is_blank_or_placeholder "$GATEWAY_TOKEN"; then
    echo "[phase:${PHASE_NAME}] ERROR: Failed to retrieve Gateway token (${SECRETS_PREFIX}/gateway-token)" >&2
    exit 1
fi
if is_blank_or_placeholder "$MEMORY_API_TOKEN"; then
    echo "[phase:${PHASE_NAME}] ERROR: Failed to retrieve memory API token (${SECRETS_PREFIX}/memory-api-token)" >&2
    exit 1
fi
if is_blank_or_placeholder "$SANDBOX_WORKER_TOKEN"; then
    echo "[phase:${PHASE_NAME}] ERROR: Failed to retrieve sandbox worker token (${SECRETS_PREFIX}/sandbox-worker-token)" >&2
    exit 1
fi

if ! is_managed_memory_backend && is_blank_or_placeholder "$TS_HARNESS_DB_PASSWORD"; then
    echo "[phase:${PHASE_NAME}] ERROR: ${SECRETS_PREFIX}/db-password must be populated before bootstrap." >&2
    echo "[phase:${PHASE_NAME}] Set a persistent ${SECRETS_PREFIX}/db-password in Secrets Manager." >&2
    exit 1
fi
if is_managed_memory_backend && is_blank_or_placeholder "$TS_HARNESS_DB_PASSWORD"; then
    echo "[phase:${PHASE_NAME}] ERROR: Managed DB secret at ${MANAGED_DB_CREDENTIAL_REF} did not yield a password." >&2
    exit 1
fi

# CODING_BACKEND_* must be real values. write_runtime_env_file skips blanks, which
# silently drops them from runtime .env and breaks coding_backend_readiness.
if [[ -z "${CODING_BACKEND_API_KEY}" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: CODING_BACKEND_API_KEY missing — ${SECRETS_PREFIX}/coding-backend-api-key is empty or still the Terraform placeholder (\" \")." >&2
    echo "[phase:${PHASE_NAME}] Reason: Secrets Manager has no usable API key, so phase 12/14 will omit CODING_BACKEND_API_KEY from runtime .env." >&2
    echo "[phase:${PHASE_NAME}] Fix: put a real provider API key in ${SECRETS_PREFIX}/coding-backend-api-key, then re-run this phase (terraform apply will not overwrite populated values)." >&2
    exit 1
fi
if [[ -z "${CODING_BACKEND_PROVIDER}" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: CODING_BACKEND_PROVIDER missing — ${SECRETS_PREFIX}/coding-backend-provider is empty or still the Terraform placeholder (\" \")." >&2
    echo "[phase:${PHASE_NAME}] Reason: Secrets Manager has no usable provider id (e.g. anthropic, openai), so phase 12/14 will omit CODING_BACKEND_PROVIDER from runtime .env." >&2
    echo "[phase:${PHASE_NAME}] Fix: put a real provider name in ${SECRETS_PREFIX}/coding-backend-provider, then re-run this phase." >&2
    exit 1
fi
if [[ -z "${GITHUB_TOKEN}" ]]; then
    echo "[phase:${PHASE_NAME}] WARNING: GITHUB_TOKEN missing — ${SECRETS_PREFIX}/github-token is empty/placeholder; clone/push/PR will fail until populated." >&2
fi
echo "[phase:${PHASE_NAME}] CODING_BACKEND_PROVIDER=${CODING_BACKEND_PROVIDER} (api key present; model=${CODING_BACKEND_MODEL:-default})"

echo "[phase:${PHASE_NAME}] Secrets retrieved successfully."

# Write secrets to a mode-600 env file for subsequent phases to source.
mkdir -p "$DATA_DIR/.setup"
{
    write_env_var ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
    write_env_var GATEWAY_TOKEN "$GATEWAY_TOKEN"
    write_env_var MEMORY_API_TOKEN "$MEMORY_API_TOKEN"
    write_env_var SANDBOX_WORKER_TOKEN "$SANDBOX_WORKER_TOKEN"
    write_env_var CODING_JOB_SECRET "$CODING_JOB_SECRET"
    write_env_var LLM_PROXY_SECRET "$LLM_PROXY_SECRET"
    write_env_var GITHUB_APP_WEBHOOK_SECRET "$GITHUB_APP_WEBHOOK_SECRET"
    write_env_var GITHUB_APP_PRIVATE_KEY "$GITHUB_APP_PRIVATE_KEY"
    write_env_var TS_HARNESS_DB_PASSWORD "$TS_HARNESS_DB_PASSWORD"
    write_env_var MANAGED_DB_PASSWORD "${MANAGED_DB_PASSWORD:-$TS_HARNESS_DB_PASSWORD}"
    write_env_var MANAGED_DB_CREDENTIAL_REF "${MANAGED_DB_CREDENTIAL_REF:-}"
    write_env_var MANAGED_DB_HOST "${MANAGED_DB_HOST:-}"
    write_env_var MANAGED_DB_PORT "${MANAGED_DB_PORT:-5432}"
    write_env_var MANAGED_DB_NAME "${MANAGED_DB_NAME:-}"
    write_env_var CLOUD_PROVIDER "${CLOUD_PROVIDER:-aws}"
    write_env_var CLOUD_REGION "${CLOUD_REGION:-${AWS_REGION:-}}"
    write_env_var SLACK_BOT_TOKEN "$SLACK_BOT_TOKEN"
    write_env_var SLACK_APP_TOKEN "$SLACK_APP_TOKEN"
    write_env_var SLACK_ALLOWED_IDS "$SLACK_ALLOWED_IDS"
    write_env_var SLACK_SIGNING_SECRET "$SLACK_SIGNING_SECRET"
    write_env_var OPENAI_API_KEY "$OPENAI_API_KEY"
    write_env_var VOYAGE_API_KEY "$VOYAGE_API_KEY"
    write_env_var GEMINI_API_KEY "$GEMINI_API_KEY"
    write_env_var MISTRAL_API_KEY "$MISTRAL_API_KEY"
    write_env_var GOOGLE_OAUTH_CLIENT_ID "$GOOGLE_OAUTH_CLIENT_ID"
    write_env_var GOOGLE_OAUTH_CLIENT_SECRET "$GOOGLE_OAUTH_CLIENT_SECRET"
    write_env_var GOOGLE_OAUTH_CALLBACK_URL "$GOOGLE_OAUTH_CALLBACK_URL"
    write_env_var AGENT_CREDENTIALS_KMS_KEY_ID "${AGENT_CREDENTIALS_KMS_KEY_ID:-${CREDENTIALS_ENCRYPTION_KEY_ID:-}}"
    write_env_var CREDENTIALS_ENCRYPTION_KEY_ID "${CREDENTIALS_ENCRYPTION_KEY_ID:-${AGENT_CREDENTIALS_KMS_KEY_ID:-}}"
    write_env_var GOOGLE_WORKSPACE_DEFAULT_EMAIL "$GOOGLE_WORKSPACE_DEFAULT_EMAIL"
    write_env_var GOOGLE_COMMS_EMAIL "$GOOGLE_COMMS_EMAIL"
    write_env_var GOOGLE_MARKETING_EMAIL "$GOOGLE_MARKETING_EMAIL"
    write_env_var GOOGLE_SALES_EMAIL "$GOOGLE_SALES_EMAIL"
    write_env_var GOOGLE_PM_EMAIL "$GOOGLE_PM_EMAIL"
    write_env_var GOOGLE_PM_PRD_FOLDER_ID "$GOOGLE_PM_PRD_FOLDER_ID"
    write_env_var GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID "$GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID"
    write_env_var GOOGLE_WORKSPACE_SA_JSON_PATH "$GOOGLE_WORKSPACE_SA_JSON_PATH"
    write_env_var CODING_BACKEND_API_KEY "$CODING_BACKEND_API_KEY"
    write_env_var CODING_BACKEND_PROVIDER "$CODING_BACKEND_PROVIDER"
    write_env_var CODING_BACKEND_MODEL "$CODING_BACKEND_MODEL"
    write_env_var GITHUB_TOKEN "$GITHUB_TOKEN"
    write_env_var ATLASSIAN_SITE_URL "$ATLASSIAN_SITE_URL"
    write_env_var ATLASSIAN_EMAIL "$ATLASSIAN_EMAIL"
    write_env_var ATLASSIAN_API_TOKEN "$ATLASSIAN_API_TOKEN"
    write_env_var ATLASSIAN_CLIENT_ID "$ATLASSIAN_CLIENT_ID"
    write_env_var ATLASSIAN_CLIENT_SECRET "$ATLASSIAN_CLIENT_SECRET"
    write_env_var ATLASSIAN_CLOUD_ID "$ATLASSIAN_CLOUD_ID"
} > "$SECRETS_ENV"
chmod 600 "$SECRETS_ENV"
echo "[phase:${PHASE_NAME}] Secrets written to $SECRETS_ENV (mode 600)."

write_phase_marker "$PHASE_DONE" "secrets-refetched"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
