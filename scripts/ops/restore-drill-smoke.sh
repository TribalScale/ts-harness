#!/usr/bin/env bash
# Restore drill.
#   restore-drill-smoke.sh                  non-destructive readiness checks (config + pg_isready)
#   restore-drill-smoke.sh --rds            restore the latest automated RDS snapshot to a temporary
#                                           instance, verify schema and core tables, then delete it
#   restore-drill-smoke.sh --rds --dry-run  print the aws/psql calls without executing them
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="readiness"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --rds) MODE="rds" ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,6p' "$0"
      exit 0
      ;;
    *)
      echo "[restore-drill] unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

readiness_checks() {
  echo "[restore-drill] Starting non-destructive restore readiness checks"

  if [[ -n "${TS_HARNESS_CONFIG:-}" && -f "${TS_HARNESS_CONFIG}" ]]; then
    echo "[restore-drill] runtime config present: ${TS_HARNESS_CONFIG}"
  else
    echo "[restore-drill] WARNING: TS_HARNESS_CONFIG missing or unreadable" >&2
  fi

  if command -v pg_isready >/dev/null 2>&1; then
    PGHOST="${TS_HARNESS_DB_HOST:-${TS_HARNESS_DB_HOST:-localhost}}"
    PGPORT="${TS_HARNESS_DB_PORT:-${TS_HARNESS_DB_PORT:-5432}}"
    if pg_isready -h "$PGHOST" -p "$PGPORT" >/dev/null 2>&1; then
      echo "[restore-drill] database reachable at ${PGHOST}:${PGPORT}"
    else
      echo "[restore-drill] WARNING: database not reachable at ${PGHOST}:${PGPORT}" >&2
      exit 1
    fi
  else
    echo "[restore-drill] pg_isready not installed; skipping DB probe"
  fi

  echo "[restore-drill] OK"
}

if [[ "$MODE" == "readiness" ]]; then
  readiness_checks
  exit 0
fi

# ── --rds: real snapshot restore ─────────────────────────────────────────────

RDS_INSTANCE_ID="${RDS_INSTANCE_ID:-}"
if [[ -z "$RDS_INSTANCE_ID" ]]; then
  echo "[restore-drill] RDS_INSTANCE_ID is required for --rds" >&2
  exit 2
fi
MIGRATIONS_DIR="${MIGRATIONS_DIR:-$SCRIPT_DIR/../../database/migrations}"
DRILL_ID="${DRILL_INSTANCE_ID:-${RDS_INSTANCE_ID}-drill-$(date -u +%Y%m%d%H%M%S)}"
CORE_TABLES=(delivery_programs program_external_refs workflow_decisions program_releases workflow_tasks)
DRILL_CREATED=0
FAILURES=0

log() { echo "[restore-drill] $*"; }
fail() {
  echo "[restore-drill] FAIL: $*" >&2
  FAILURES=$((FAILURES + 1))
}

# aws_q PLACEHOLDER ARGS...: run an aws query, or print it and return PLACEHOLDER in dry-run.
aws_q() {
  local placeholder="$1"
  shift
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] aws $*" >&2
    echo "$placeholder"
    return 0
  fi
  aws "$@"
}

aws_do() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] aws $*" >&2
    return 0
  fi
  aws "$@"
}

cleanup() {
  local status=$?
  if [[ "$DRILL_CREATED" == "1" ]]; then
    log "deleting drill instance ${DRILL_ID}"
    if ! aws_do rds delete-db-instance \
      --db-instance-identifier "$DRILL_ID" \
      --skip-final-snapshot \
      --delete-automated-backups >/dev/null; then
      echo "[restore-drill] WARNING: delete of ${DRILL_ID} failed; remove it manually" >&2
      status=1
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

log "source instance: ${RDS_INSTANCE_ID}"
# shellcheck disable=SC2016  # JMESPath backtick literals, not shell expansions.
read -r SUBNET_GROUP SECURITY_GROUPS INSTANCE_CLASS < <(
  aws_q "<subnet-group> <sg-ids> <instance-class>" rds describe-db-instances \
    --db-instance-identifier "$RDS_INSTANCE_ID" \
    --query 'DBInstances[0].[DBSubnetGroup.DBSubnetGroupName, join(`,`, VpcSecurityGroups[].VpcSecurityGroupId), DBInstanceClass]' \
    --output text
)
if [[ -z "${SUBNET_GROUP:-}" || -z "${SECURITY_GROUPS:-}" || -z "${INSTANCE_CLASS:-}" ]]; then
  echo "[restore-drill] could not describe ${RDS_INSTANCE_ID} (subnet group, security groups, class)" >&2
  exit 1
fi
# shellcheck disable=SC2016
SNAPSHOT_ID="$(
  aws_q "<latest-automated-snapshot>" rds describe-db-snapshots \
    --db-instance-identifier "$RDS_INSTANCE_ID" \
    --snapshot-type automated \
    --query 'reverse(sort_by(DBSnapshots[?Status==`available`], &SnapshotCreateTime))[0].DBSnapshotIdentifier' \
    --output text
)"
if [[ -z "$SNAPSHOT_ID" || "$SNAPSHOT_ID" == "None" ]]; then
  echo "[restore-drill] no available automated snapshot for ${RDS_INSTANCE_ID}" >&2
  exit 1
fi
log "latest automated snapshot: ${SNAPSHOT_ID}"

IFS=',' read -r -a SG_IDS <<< "$SECURITY_GROUPS"
DRILL_CREATED=1
log "restoring ${SNAPSHOT_ID} to ${DRILL_ID} (subnet group ${SUBNET_GROUP}, not publicly accessible)"
aws_do rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier "$DRILL_ID" \
  --db-snapshot-identifier "$SNAPSHOT_ID" \
  --db-subnet-group-name "$SUBNET_GROUP" \
  --vpc-security-group-ids "${SG_IDS[@]}" \
  --db-instance-class "${DRILL_INSTANCE_CLASS:-$INSTANCE_CLASS}" \
  --no-publicly-accessible \
  --no-multi-az \
  --no-deletion-protection \
  --manage-master-user-password \
  --tags Key=purpose,Value=restore-drill >/dev/null

log "waiting for ${DRILL_ID} to become available"
aws_do rds wait db-instance-available --db-instance-identifier "$DRILL_ID"

read -r DB_HOST DB_PORT DB_USER DB_NAME SECRET_ARN < <(
  aws_q "<drill-endpoint> 5432 <master-user> <db-name> <secret-arn>" rds describe-db-instances \
    --db-instance-identifier "$DRILL_ID" \
    --query 'DBInstances[0].[Endpoint.Address, Endpoint.Port, MasterUsername, DBName, MasterUserSecret.SecretArn]' \
    --output text
)
if [[ -z "${DB_HOST:-}" || -z "${SECRET_ARN:-}" || "${SECRET_ARN}" == "None" ]]; then
  echo "[restore-drill] ${DRILL_ID} has no endpoint or managed master secret" >&2
  exit 1
fi
DB_PASSWORD="$(
  aws_q "<password>" secretsmanager get-secret-value \
    --secret-id "$SECRET_ARN" \
    --query SecretString \
    --output text | python3 -c 'import json,sys; raw=sys.stdin.read(); print(json.loads(raw)["password"] if raw.strip().startswith("{") else raw.strip())'
)"

drill_sql() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] psql host=${DB_HOST} dbname=${DB_NAME} -c \"$1\"" >&2
    return 0
  fi
  PGPASSWORD="$DB_PASSWORD" PGSSLMODE="${PGSSLMODE:-require}" psql \
    "host=${DB_HOST} port=${DB_PORT} user=${DB_USER} dbname=${DB_NAME}" \
    -v ON_ERROR_STOP=1 -tA -c "$1"
}

log "checking schema_migrations against ${MIGRATIONS_DIR}"
if applied="$(drill_sql "SELECT filename FROM schema_migrations ORDER BY filename")"; then
  if [[ "$DRY_RUN" != "1" ]]; then
    missing=0
    for path in "$MIGRATIONS_DIR"/*.sql; do
      file="$(basename "$path")"
      if ! grep -qxF "$file" <<< "$applied"; then
        fail "migration not recorded in restored schema_migrations: ${file}"
        missing=$((missing + 1))
      fi
    done
    if [[ "$missing" == "0" ]]; then
      log "all migrations present"
    fi
  fi
else
  fail "schema_migrations is not readable"
fi

for table in "${CORE_TABLES[@]}"; do
  if count="$(drill_sql "SELECT count(*) FROM ${table}")"; then
    log "${table}: ${count:-<dry-run>} rows"
  else
    fail "${table} is not readable"
  fi
done

if [[ "$FAILURES" -gt 0 ]]; then
  echo "[restore-drill] RDS restore drill FAILED (${FAILURES} check(s))" >&2
  exit 1
fi
log "RDS restore drill OK"
