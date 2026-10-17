#!/usr/bin/env bash
set -euo pipefail

# On-instance verification: agent-runtime receives memory-api configuration and
# Postgres is reachable.

MEMORY_BACKEND="${MEMORY_BACKEND:-local}"
ENT_DIR="${ENTERPRISE_ASSETS_DIR:-$HOME/enterprise-assets}"
ENT_DOCKER="${ENTERPRISE_DOCKER_DIR:-$ENT_DIR/docker}"

if [[ ! -d "$ENT_DIR" ]]; then
    echo "ERROR: ENTERPRISE_ASSETS_DIR not found: $ENT_DIR" >&2
    exit 1
fi

compose_args=(-f "$ENT_DOCKER/docker-compose.memory-api.yml" -f "$ENT_DOCKER/docker-compose.agent-runtime.yml")
if [[ "$MEMORY_BACKEND" == "rds" || "$MEMORY_BACKEND" == "managed" ]]; then
    compose_args+=(-f "$ENT_DOCKER/docker-compose.managed-db.yml")
elif [[ -f "$ENT_DOCKER/docker-compose.local-db.yml" ]]; then
    compose_args+=(-f "$ENT_DOCKER/docker-compose.local-db.yml")
fi

cd "$ENT_DIR"

echo "=== verify-memory-stack: MEMORY_BACKEND=$MEMORY_BACKEND ==="
echo "=== compose: docker compose ${compose_args[*]} ==="

echo ""
echo "=== agent-runtime memory-api environment ==="
if ! docker compose "${compose_args[@]}" exec -T agent-runtime true 2>/dev/null; then
    echo "ERROR: agent-runtime is not running or not reachable." >&2
    echo "Run: cd $ENT_DIR && docker compose ${compose_args[*]} ps" >&2
    exit 1
fi

docker compose "${compose_args[@]}" exec -T agent-runtime env \
    | grep -E '^(MEMORY_API_URL|TS_HARNESS_DB_)' \
    | sed 's/^\(TS_HARNESS_DB_PASSWORD=\).*/\1<redacted>/' || true

if ! docker compose "${compose_args[@]}" exec -T agent-runtime env | grep -q '^MEMORY_API_URL='; then
    echo "ERROR: agent-runtime env missing MEMORY_API_URL" >&2
    exit 1
fi

echo ""
echo "=== Postgres connectivity ==="
if [[ "$MEMORY_BACKEND" == "local" ]]; then
    docker compose "${compose_args[@]}" exec -T ts-harness-db \
        psql -U ts_harness -d ts_harness_memory -v ON_ERROR_STOP=1 -c "SELECT 1 AS ok;"
    cnt="$(docker compose "${compose_args[@]}" exec -T ts-harness-db \
        psql -U ts_harness -d ts_harness_memory -tAc \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='memories';")"
    [[ "${cnt:-0}" == "1" ]] || { echo "ERROR: expected public.memories table" >&2; exit 1; }
else
    if [[ -z "${PGHOST:-}" || -z "${PGUSER:-}" || -z "${PGDATABASE:-}" ]]; then
        echo "SKIP RDS psql check (set PGHOST PGUSER PGDATABASE; PGPASSWORD PGPORT optional)."
    else
        export PGPORT="${PGPORT:-5432}"
        export PGPASSWORD="${PGPASSWORD:?set PGPASSWORD for RDS check}"
        psql -v ON_ERROR_STOP=1 -c "SELECT 1 AS ok;"
    fi
fi

echo ""
echo "=== verify-memory-stack: agent-runtime checks passed ==="
