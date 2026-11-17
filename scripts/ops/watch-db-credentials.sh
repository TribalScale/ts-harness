#!/usr/bin/env bash
# Cron job (phase 12a). RDS rotates the managed master password every 7 days; when
# the live secret no longer matches the password memory-api runs with, run
# sync-compose-db-env.sh to refresh the host env files and recreate the DB consumers.
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$OPS_DIR/.." && pwd)"
PHASES_DIR="$SCRIPT_DIR/phases"
DATA_DIR="${DATA_DIR:-/vol/ts-harness-data}"
export SCRIPT_DIR DATA_DIR

# shellcheck disable=SC1091
source "$PHASES_DIR/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "$PHASES_DIR/lib/common.sh"

log() { echo "[db-credentials] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

exec 9>"${DB_CREDENTIALS_LOCK:-/var/lock/ts-harness-db-credentials.lock}"
flock -n 9 || exit 0

load_instance_env
resolve_cloud_neutral_env
if ! is_managed_memory_backend || [[ -z "${MANAGED_DB_CREDENTIAL_REF:-}" ]]; then
    exit 0
fi

container="$(docker ps -q --filter label=com.docker.compose.service=memory-api | head -n 1)"
if [[ -z "$container" ]]; then
    log "memory-api is not running; skipping"
    exit 0
fi
running="$(docker exec "$container" printenv TS_HARNESS_DB_PASSWORD 2>/dev/null || true)"

ensure_cloud_adapter
if ! live_json="$(cloud_fetch_managed_db_credentials "$MANAGED_DB_CREDENTIAL_REF")"; then
    log "ERROR: could not read ${MANAGED_DB_CREDENTIAL_REF}"
    exit 1
fi
live="$(printf '%s' "$live_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"

if [[ -n "$live" && "$live" == "$running" ]]; then
    exit 0
fi

log "managed DB password changed; syncing host env and recreating DB consumers"
bash "$OPS_DIR/sync-compose-db-env.sh"
log "sync complete"
