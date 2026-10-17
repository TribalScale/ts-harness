#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

PHASE_NAME="11b-agent-auth-init"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)


if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        echo "[phase:${PHASE_NAME}] Already complete for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

if [[ ! -f "$SECRETS_ENV" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $SECRETS_ENV not found — run 10-fetch-secrets.sh first" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$SECRETS_ENV"

AGENTS_BASE="$DATA_DIR/.ts-harness/agents"
ALL_AGENTS=(orchestrator dev-agent ops-agent qa-agent review-agent analyst-agent pm-agent comms-agent marketing-agent sales-agent builder-agent)

# Writes a key=value pair into an agent's auth-profiles.json (creates if absent)
write_agent_auth() {
    local agent_id="$1" key="$2" value="$3"
    local auth_file="$AGENTS_BASE/${agent_id}/agent/auth-profiles.json"
    mkdir -p "$(dirname "$auth_file")"
    if [[ ! -f "$auth_file" ]]; then
        echo '{}' > "$auth_file"
    fi
    python3 - "$auth_file" "$key" "$value" <<'EOF'
import json, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
data[key] = val
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
EOF
    chmod 600 "$auth_file"
}

# All agents get the Anthropic API key (LLM is universal)
for agent_id in "${ALL_AGENTS[@]}"; do
    if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        write_agent_auth "$agent_id" "ANTHROPIC_API_KEY" "$ANTHROPIC_API_KEY"
        echo "[phase:${PHASE_NAME}] ANTHROPIC_API_KEY -> $agent_id"
    fi
done

# dev-agent only: GitHub token
GITHUB_TOKEN_VAL="${GITHUB_TOKEN:-$(fetch_secret "github-token" true 2>/dev/null || true)}"
if [[ -n "${GITHUB_TOKEN_VAL//[[:space:]]/}" ]]; then
    write_agent_auth "dev-agent" "GITHUB_TOKEN" "$GITHUB_TOKEN_VAL"
    echo "[phase:${PHASE_NAME}] GITHUB_TOKEN -> dev-agent"
fi

# dev-agent only: coding-backend provider + API key + model (OpenCode child env)
CODING_BACKEND_API_KEY_VAL="${CODING_BACKEND_API_KEY:-$(fetch_secret "coding-backend-api-key" true 2>/dev/null || true)}"
CODING_BACKEND_PROVIDER_VAL="${CODING_BACKEND_PROVIDER:-$(fetch_secret "coding-backend-provider" true 2>/dev/null || true)}"
CODING_BACKEND_MODEL_VAL="${CODING_BACKEND_MODEL:-$(fetch_secret "coding-backend-model" true 2>/dev/null || true)}"
if [[ -n "${CODING_BACKEND_API_KEY_VAL//[[:space:]]/}" ]]; then
    write_agent_auth "dev-agent" "CODING_BACKEND_API_KEY" "$CODING_BACKEND_API_KEY_VAL"
    echo "[phase:${PHASE_NAME}] CODING_BACKEND_API_KEY -> dev-agent"
fi
if [[ -n "${CODING_BACKEND_PROVIDER_VAL//[[:space:]]/}" ]]; then
    write_agent_auth "dev-agent" "CODING_BACKEND_PROVIDER" "$CODING_BACKEND_PROVIDER_VAL"
    echo "[phase:${PHASE_NAME}] CODING_BACKEND_PROVIDER -> dev-agent"
fi
if [[ -n "${CODING_BACKEND_MODEL_VAL//[[:space:]]/}" ]]; then
    write_agent_auth "dev-agent" "CODING_BACKEND_MODEL" "$CODING_BACKEND_MODEL_VAL"
    echo "[phase:${PHASE_NAME}] CODING_BACKEND_MODEL -> dev-agent"
fi

# pm-agent only: Jira credentials
JIRA_TOKEN_VAL="${JIRA_TOKEN:-$(fetch_secret "jira-token" true 2>/dev/null || true)}"
JIRA_URL_VAL="${JIRA_URL:-$(fetch_secret "jira-url" true 2>/dev/null || true)}"
JIRA_USERNAME_VAL="${JIRA_USERNAME:-$(fetch_secret "jira-username" true 2>/dev/null || true)}"
[[ -n "${JIRA_TOKEN_VAL//[[:space:]]/}" ]]    && write_agent_auth "pm-agent" "JIRA_TOKEN" "$JIRA_TOKEN_VAL"       && echo "[phase:${PHASE_NAME}] JIRA_TOKEN -> pm-agent"
[[ -n "${JIRA_URL_VAL//[[:space:]]/}" ]]      && write_agent_auth "pm-agent" "JIRA_URL" "$JIRA_URL_VAL"           && echo "[phase:${PHASE_NAME}] JIRA_URL -> pm-agent"
[[ -n "${JIRA_USERNAME_VAL//[[:space:]]/}" ]] && write_agent_auth "pm-agent" "JIRA_USERNAME" "$JIRA_USERNAME_VAL" && echo "[phase:${PHASE_NAME}] JIRA_USERNAME -> pm-agent"

# orchestrator + comms-agent + marketing-agent + sales-agent: Slack tokens
for agent_id in orchestrator comms-agent marketing-agent sales-agent; do
    [[ -n "${SLACK_BOT_TOKEN:-}" ]] && write_agent_auth "$agent_id" "SLACK_BOT_TOKEN" "$SLACK_BOT_TOKEN" && echo "[phase:${PHASE_NAME}] SLACK_BOT_TOKEN -> $agent_id"
    [[ -n "${SLACK_APP_TOKEN:-}" ]] && write_agent_auth "$agent_id" "SLACK_APP_TOKEN" "$SLACK_APP_TOKEN" && echo "[phase:${PHASE_NAME}] SLACK_APP_TOKEN -> $agent_id"
done

# orchestrator only: Telegram + Discord tokens
TELEGRAM_TOKEN_VAL="${TELEGRAM_TOKEN:-$(fetch_secret "telegram-token" true 2>/dev/null || true)}"
DISCORD_TOKEN_VAL="${DISCORD_TOKEN:-$(fetch_secret "discord-token" true 2>/dev/null || true)}"
[[ -n "${TELEGRAM_TOKEN_VAL//[[:space:]]/}" ]] && write_agent_auth "orchestrator" "TELEGRAM_TOKEN" "$TELEGRAM_TOKEN_VAL" && echo "[phase:${PHASE_NAME}] TELEGRAM_TOKEN -> orchestrator"
[[ -n "${DISCORD_TOKEN_VAL//[[:space:]]/}" ]]  && write_agent_auth "orchestrator" "DISCORD_TOKEN" "$DISCORD_TOKEN_VAL"  && echo "[phase:${PHASE_NAME}] DISCORD_TOKEN -> orchestrator"

# Embedding provider env var: written to ALL agents (all have memory skill)
# Uses the provider-native key name that the agent runtime already reads.
ACTIVE_PROVIDER="${EMBEDDINGS_PROVIDER:-openai}"
case "$ACTIVE_PROVIDER" in
    openai)  EMBED_KEY="OPENAI_API_KEY";  EMBED_VAL="$(fetch_secret "openai-api-key" true 2>/dev/null || true)" ;;
    voyage)  EMBED_KEY="VOYAGE_API_KEY";  EMBED_VAL="$(fetch_secret "voyage-api-key" true 2>/dev/null || true)" ;;
    gemini)  EMBED_KEY="GEMINI_API_KEY";  EMBED_VAL="$(fetch_secret "gemini-api-key" true 2>/dev/null || true)" ;;
    mistral) EMBED_KEY="MISTRAL_API_KEY"; EMBED_VAL="$(fetch_secret "mistral-api-key" true 2>/dev/null || true)" ;;
    bedrock|local)
        EMBED_KEY=""; EMBED_VAL=""
        echo "[phase:${PHASE_NAME}] Embedding provider '$ACTIVE_PROVIDER' uses credential chain; no stored key needed."
        ;;
    *)
        EMBED_KEY=""; EMBED_VAL=""
        echo "[phase:${PHASE_NAME}] WARNING: Unknown EMBEDDINGS_PROVIDER '$ACTIVE_PROVIDER'; skipping embedding key distribution."
        ;;
esac

if [[ -n "$EMBED_KEY" && -n "${EMBED_VAL//[[:space:]]/}" ]]; then
    for agent_id in "${ALL_AGENTS[@]}"; do
        write_agent_auth "$agent_id" "$EMBED_KEY" "$EMBED_VAL"
        echo "[phase:${PHASE_NAME}] $EMBED_KEY -> $agent_id"
    done
fi

# Set ownership on all agentDir directories (ALL_AGENTS already includes marketing-agent and sales-agent)
for agent_id in "${ALL_AGENTS[@]}"; do
    agent_dir="$AGENTS_BASE/${agent_id}/agent"
    [[ -d "$agent_dir" ]] && chown -R "${ADMIN_USER}:${ADMIN_USER}" "$agent_dir"
done

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
