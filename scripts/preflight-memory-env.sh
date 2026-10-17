#!/usr/bin/env bash
# Validate TS_HARNESS_DB_* before docker compose for TS Harness.
# Usage: MEMORY_BACKEND=local|rds|managed [TS_HARNESS_DB_*...] ./preflight-memory-env.sh
set -euo pipefail

MEMORY_BACKEND="${MEMORY_BACKEND:-local}"
SECRETS_PREFIX="${SECRETS_PREFIX:-<project>/secrets}"
MULTI_AGENT_ENABLED="${MULTI_AGENT_ENABLED:-false}"
EMBEDDINGS_PROVIDER="${EMBEDDINGS_PROVIDER:-openai}"

fail() {
    echo "ERROR: preflight-memory-env (MEMORY_BACKEND=$MEMORY_BACKEND): $*" >&2
    exit 1
}

looks_placeholder() {
    local v="${1:-}"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    [[ -z "$v" ]] && return 0
    local lc
    lc="$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')"
    [[ "$lc" == *"placeholder"* ]] && return 0
    [[ "$lc" == *"changeme"* ]] && return 0
    [[ "$lc" == *"replace_me"* ]] && return 0
    [[ "$lc" == *"todo"* ]] && return 0
    [[ "$lc" == *"fixme"* ]] && return 0
    [[ "$v" == "generate_or_set_via_secrets_manager" ]] && return 0
    [[ "$v" == "your_secure_gateway_token" ]] && return 0
    if [[ "$v" =~ ^your_ ]] || [[ "$v" =~ Your_ ]]; then
        return 0
    fi
    return 1
}

case "$MEMORY_BACKEND" in
    local)
        looks_placeholder "${TS_HARNESS_DB_PASSWORD:-}" && fail "TS_HARNESS_DB_PASSWORD is missing or looks like a template value — set a real secret at ${SECRETS_PREFIX}/db-password (or export TS_HARNESS_DB_PASSWORD for compose on a dev host)."
        ;;
    rds|managed)
        [[ -z "${TS_HARNESS_DB_HOST:-}" ]] && fail "TS_HARNESS_DB_HOST is required for memory_backend=${MEMORY_BACKEND}"
        [[ -z "${TS_HARNESS_DB_PORT:-}" ]] && fail "TS_HARNESS_DB_PORT is required for memory_backend=${MEMORY_BACKEND}"
        [[ -z "${TS_HARNESS_DB_USER:-}" ]] && fail "TS_HARNESS_DB_USER is required for memory_backend=${MEMORY_BACKEND}"
        [[ -z "${TS_HARNESS_DB_NAME:-}" ]] && fail "TS_HARNESS_DB_NAME is required for memory_backend=${MEMORY_BACKEND}"
        looks_placeholder "${TS_HARNESS_DB_PASSWORD:-}" && fail "TS_HARNESS_DB_PASSWORD is missing or looks like a template value — check managed DB secret / rotation."
        ;;
    *)
        fail "MEMORY_BACKEND must be 'local', 'rds', or 'managed' (got: $MEMORY_BACKEND)"
        ;;
esac

if [[ "$MULTI_AGENT_ENABLED" == "true" && "$EMBEDDINGS_PROVIDER" == "local" ]]; then
    fail "local embeddings provider is not supported by memory-api; use a cloud provider or disable multi-agent memory-api"
fi

echo "preflight-memory-env: OK (MEMORY_BACKEND=$MEMORY_BACKEND)"
