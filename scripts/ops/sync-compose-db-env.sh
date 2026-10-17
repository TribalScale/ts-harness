#!/usr/bin/env bash
# Refresh managed DB password from the live cloud secret into compose-env,
# secrets.env, and runtime .env, then recreate memory-api.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
PHASES_DIR="$SCRIPT_DIR/phases"
DATA_DIR="${DATA_DIR:-/vol/ts-harness-data}"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
COMPOSE_ENV="$DATA_DIR/.setup/compose-env.sh"
ADMIN_USER="${ADMIN_USER:-artemis}"
ENTERPRISE_ASSETS_DIR="${ENTERPRISE_ASSETS_DIR:-/home/${ADMIN_USER}/enterprise-assets}"
ENTERPRISE_DOCKER_DIR="${ENTERPRISE_DOCKER_DIR:-$ENTERPRISE_ASSETS_DIR/docker}"
TS_HARNESS_CONFIG_DIR="${TS_HARNESS_CONFIG_DIR:-$DATA_DIR/.ts-harness}"
RUNTIME_ENV="$TS_HARNESS_CONFIG_DIR/.env"

export SCRIPT_DIR DATA_DIR ENTERPRISE_ASSETS_DIR ENTERPRISE_DOCKER_DIR TS_HARNESS_CONFIG_DIR ADMIN_USER COMPOSE_ENV RUNTIME_ENV

# shellcheck disable=SC1091
source "$PHASES_DIR/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"

echo "=== Sync compose DB password from managed DB secret ==="
if [[ ! -f "$SECRETS_ENV" ]]; then
    echo "ERROR: $SECRETS_ENV not found — run scripts/phases/10-fetch-secrets.sh first" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$SECRETS_ENV"
load_instance_env
resolve_cloud_neutral_env

if is_managed_memory_backend; then
    if [[ -z "${MANAGED_DB_CREDENTIAL_REF:-}" ]]; then
        echo "ERROR: MEMORY_BACKEND=${MEMORY_BACKEND} but MANAGED_DB_CREDENTIAL_REF (or RDS_SECRET_ARN) is empty" >&2
        exit 1
    fi
    ensure_cloud_adapter
    echo "Fetching live DB credentials from ${MANAGED_DB_CREDENTIAL_REF}"
    MANAGED_DB_JSON="$(cloud_fetch_managed_db_credentials "$MANAGED_DB_CREDENTIAL_REF")"
    TS_HARNESS_DB_PASSWORD="$(printf '%s' "$MANAGED_DB_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
    TS_HARNESS_DB_USER="$(printf '%s' "$MANAGED_DB_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("username",""))')"
    export TS_HARNESS_DB_PASSWORD TS_HARNESS_DB_USER
    export TS_HARNESS_DB_HOST="${TS_HARNESS_DB_HOST:-${MANAGED_DB_HOST:-}}"
    export TS_HARNESS_DB_PORT="${TS_HARNESS_DB_PORT:-${MANAGED_DB_PORT:-5432}}"
    export TS_HARNESS_DB_NAME="${TS_HARNESS_DB_NAME:-${MANAGED_DB_NAME:-ts_harness}}"
    export PGSSLMODE="${PGSSLMODE:-require}"
else
    export TS_HARNESS_DB_PASSWORD
fi
if [[ -z "${TS_HARNESS_DB_PASSWORD//[[:space:]]/}" ]]; then
    echo "ERROR: TS_HARNESS_DB_PASSWORD is empty after credential refresh" >&2
    exit 1
fi

sync_compose_db_password "$COMPOSE_ENV" "$TS_HARNESS_DB_PASSWORD"
persist_env_key "$SECRETS_ENV" TS_HARNESS_DB_PASSWORD "$TS_HARNESS_DB_PASSWORD"
persist_env_key "$SECRETS_ENV" MANAGED_DB_PASSWORD "$TS_HARNESS_DB_PASSWORD"

export TS_HARNESS_TOOLS_ROOT="${TS_HARNESS_TOOLS_ROOT:-/tools}"
write_runtime_env_file "$RUNTIME_ENV"
echo "Updated $COMPOSE_ENV $SECRETS_ENV $RUNTIME_ENV"

set -a
# shellcheck disable=SC1090
source "$COMPOSE_ENV"
set +a

cd "$ENTERPRISE_DOCKER_DIR"
COMPOSE=(docker compose --env-file "$RUNTIME_ENV"
    -f docker-compose.memory-api.yml
    -f docker-compose.agent-runtime.yml
    -f docker-compose.workflow-runner.yml
    -f docker-compose.admin-ui.yml)

echo "=== Recreate memory-api ==="
"${COMPOSE[@]}" up -d --no-deps --force-recreate memory-api
sleep 4

echo "=== Verify DB connectivity from memory-api ==="
docker exec docker-memory-api-1 python3 -c "
import os, psycopg
with psycopg.connect(
    host=os.environ['TS_HARNESS_DB_HOST'],
    port=os.environ.get('TS_HARNESS_DB_PORT', '5432'),
    dbname=os.environ['TS_HARNESS_DB_NAME'],
    user=os.environ['TS_HARNESS_DB_USER'],
    password=os.environ['TS_HARNESS_DB_PASSWORD'],
    sslmode=os.environ.get('PGSSLMODE', 'require'),
    connect_timeout=10,
) as conn:
    with conn.cursor() as cur:
        cur.execute('SELECT 1')
        print('memory-api db ok', cur.fetchone())
"

echo "=== Done ==="
