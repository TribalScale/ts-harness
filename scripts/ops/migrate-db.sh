#!/usr/bin/env bash
# Apply database migrations in order with schema_migrations tracking.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MIGRATIONS_DIR="${REPO_ROOT}/database/migrations"

DB_URL="${TS_HARNESS_SESSION_DB_URL:-${DATABASE_URL:-}}"
if [[ -z "${DB_URL}" ]]; then
  echo "ERROR: set TS_HARNESS_SESSION_DB_URL or DATABASE_URL" >&2
  exit 1
fi

ORDER=(
  001_init_memory.sql
  002_init_sessions.sql
  002_workflow_tasks.sql
  003_cost_tracking.sql
  004_session_audit.sql
  005_workflow_engine_readiness.sql
  006_workflow_runner.sql
  007_tenant_isolation.sql
  008_workflows.sql
  009_scim.sql
  010_workflow_compensation.sql
  011_workflow_task_contracts.sql
  011_retry_backoff.sql
  012_runtime_transcripts.sql
  013_workflows_source_channel.sql
  014_workflow_wait_tokens.sql
  015_workflow_superseded.sql
  016_workflow_wait_audit_actions.sql
  017_workflow_lifecycle_audit_actions.sql
  018_google_oauth_credentials.sql
  019_delivery_programs.sql
  020_delivery_program_audit_actions.sql
  021_workflow_program_link_audit_action.sql
  022_program_definition_pinning.sql
  023_program_project_memory.sql
  024_transcript_fts.sql
  025_program_hierarchy.sql
  026_workflow_decisions.sql
  027_program_releases.sql
  028_audit_details_program_index.sql
  029_runner_leader_lease.sql
  030_program_department.sql
  031_teams_directory.sql
  032_program_access.sql
  033_coding_jobs.sql
  034_projects_registries.sql
)

psql "${DB_URL}" -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

for file in "${ORDER[@]}"; do
  path="${MIGRATIONS_DIR}/${file}"
  if [[ ! -f "${path}" ]]; then
    echo "WARN: missing migration ${file}, skipping" >&2
    continue
  fi
  applied="$(psql "${DB_URL}" -tAc "SELECT 1 FROM schema_migrations WHERE filename='${file}'" 2>/dev/null || true)"
  if [[ "${applied}" == "1" ]]; then
    echo "SKIP: ${file} (already applied)"
    continue
  fi
  echo "APPLY: ${file}"
  psql "${DB_URL}" -v ON_ERROR_STOP=1 -f "${path}"
  psql "${DB_URL}" -v ON_ERROR_STOP=1 -c "INSERT INTO schema_migrations (filename) VALUES ('${file}')"
done

echo "Migrations complete."
