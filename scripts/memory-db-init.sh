#!/bin/bash
set -euo pipefail

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-ts_harness}"
PGPASSWORD="${PGPASSWORD:-ts_harness}"
PGDATABASE="${PGDATABASE:-ts_harness_memory}"

export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MIGRATIONS_DIR="$SCRIPT_DIR/../database/migrations"
SEED_DIR="$SCRIPT_DIR/../database/seed"

echo "=== Memory Database Init ==="
echo "  Host:     $PGHOST:$PGPORT"
echo "  Database: $PGDATABASE"
echo "  User:     $PGUSER"

# ---- Wait for Postgres readiness ----
echo "Waiting for Postgres to be ready..."
MAX_WAIT=60
ELAPSED=0
INTERVAL=2

while ! pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -q 2>/dev/null; do
    if (( ELAPSED >= MAX_WAIT )); then
        echo "ERROR: Postgres not ready after ${MAX_WAIT}s"
        exit 1
    fi
    sleep "$INTERVAL"
    (( ELAPSED += INTERVAL ))
    echo "  ...waited ${ELAPSED}s"
done
echo "Postgres is ready (waited ${ELAPSED}s)."

run_sql_dir() {
    local dir="$1" label="$2"
    if [[ ! -d "$dir" ]]; then
        echo "No $label directory at $dir; skipping."
        return 0
    fi

    local sql_files=()
    while IFS= read -r -d '' f; do
        sql_files+=("$f")
    done < <(find "$dir" -maxdepth 1 -name '*.sql' -print0 | sort -z)

    if (( ${#sql_files[@]} == 0 )); then
        echo "No .sql files in $dir; skipping $label."
        return 0
    fi

    echo "Running ${#sql_files[@]} $label file(s) from $dir..." 
    local success=0 fail=0
    for sql_file in "${sql_files[@]}"; do
        local basename
        basename="$(basename "$sql_file")"
        echo "  Processing: $basename ..." 
        if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
                -v ON_ERROR_STOP=1 -f "$sql_file" > /tmp/psql_output_$$.log 2>&1; then
            cat /tmp/psql_output_$$.log
            echo "  $basename: OK"
            success=$((success + 1))
        else
            cat /tmp/psql_output_$$.log
            echo "  $basename: FAILED"
            fail=$((fail + 1))
        fi
        rm -f /tmp/psql_output_$$.log
    done
    echo "$label complete: $success succeeded, $fail failed."

    if (( fail > 0 )); then
        return 1
    fi
}

# ---- Run migrations ----
run_sql_dir "$MIGRATIONS_DIR" "migrations"

# ---- Run seed data ----
echo "Starting seed data load..."
run_sql_dir "$SEED_DIR" "seed"
echo "Seed data load complete."

# ---- Verify embedding column matches enterprise contract ----
EXPECTED_DIM="${ENTERPRISE_MEMORY_EMBEDDING_DIM:-1536}"
EXPECTED_TYPE="vector(${EXPECTED_DIM})"
echo "=== Verifying memories.embedding type (expect ${EXPECTED_TYPE}) ==="

# Use timeout to prevent hanging, and capture both stdout and stderr for debugging
ACTUAL="$(
    timeout 10 psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -tAc "
        SELECT COALESCE((
            SELECT format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON a.attrelid = c.oid
            WHERE c.relname = 'memories'
              AND a.attname = 'embedding'
              AND a.attnum > 0
              AND NOT a.attisdropped
        ), '');
    " 2>&1
)" || {
    echo "ERROR: Verification query failed or timed out. Check database connectivity and authentication."
    echo "  PGHOST: $PGHOST"
    echo "  PGPORT: $PGPORT"
    echo "  PGUSER: $PGUSER"
    echo "  PGDATABASE: $PGDATABASE"
    exit 1
}
ACTUAL="${ACTUAL//$'\r'/}"
ACTUAL="${ACTUAL// /}"

if [[ -z "$ACTUAL" || "$ACTUAL" != "$EXPECTED_TYPE" ]]; then
    echo "ERROR: Expected memories.embedding type '${EXPECTED_TYPE}', got: '${ACTUAL:-<missing>}'"
    echo "Set ENTERPRISE_MEMORY_EMBEDDING_DIM to match your migration or fix database/migrations."
    exit 1
fi
echo "  OK: ${ACTUAL}"

echo "=== Memory Database Init Complete ==="
