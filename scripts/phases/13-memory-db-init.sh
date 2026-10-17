#!/bin/bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"


PHASE_NAME="13-memory-db-init"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
COMPOSE_ENV="$DATA_DIR/.setup/compose-env.sh"


if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^agent_runtime_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "${AGENT_RUNTIME_VERSION:-}" ]]; then
        echo "[phase:${PHASE_NAME}] Already complete for $AGENT_RUNTIME_VERSION. Skipping."
        exit 0
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
start_ts=$(date +%s%3N)

# Source secrets and compose env (provides TS_HARNESS_DB_* and TS_HARNESS_DB_PASSWORD).
for env_file in "$SECRETS_ENV" "$COMPOSE_ENV"; do
    if [[ ! -f "$env_file" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: $env_file not found — prior phase did not complete" >&2
        exit 1
    fi
    # shellcheck disable=SC1090
    source "$env_file"
done

echo "[phase:${PHASE_NAME}] Initializing memory database (memory_backend=$MEMORY_BACKEND)..."

if [[ "$MEMORY_BACKEND" == "rds" ]]; then
    export PGHOST="$TS_HARNESS_DB_HOST"
    export PGPORT="${TS_HARNESS_DB_PORT:-5432}"
    normalize_db_host_port PGHOST PGPORT
    export PGUSER="$TS_HARNESS_DB_USER"
    export PGPASSWORD="$TS_HARNESS_DB_PASSWORD"
    export PGDATABASE="${TS_HARNESS_DB_NAME:-${RDS_DB_NAME:-ts_harness}}"
else
    export PGHOST="127.0.0.1"
    export PGPORT="5432"
    export PGUSER="ts_harness"
    export PGPASSWORD="$TS_HARNESS_DB_PASSWORD"
    export PGDATABASE="ts_harness_memory"
fi

DB_INIT_SCRIPT="$SCRIPT_DIR/memory-db-init.sh"
if [[ ! -f "$DB_INIT_SCRIPT" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $DB_INIT_SCRIPT not found" >&2
    exit 1
fi
chmod +x "$DB_INIT_SCRIPT"
"$DB_INIT_SCRIPT"

echo "[phase:${PHASE_NAME}] Memory database initialized."

mkdir -p "$DATA_DIR/.setup"
printf 'agent_runtime_version=%s\ncompleted_at=%s\n' "${AGENT_RUNTIME_VERSION:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PHASE_DONE"
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N) - start_ts ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
