-- 034_projects_registries.sql
-- Minimal projects (team ownership, worker sizing) and per-project package
-- registries. Registry credentials live in the cloud secret store; this table
-- keeps only a reference. One project is backfilled per distinct
-- delivery_programs.project_id, which becomes the project key.

BEGIN;

CREATE TABLE IF NOT EXISTS projects (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           TEXT        NOT NULL DEFAULT 'default',
    team_id             UUID        REFERENCES teams(id) ON DELETE SET NULL,
    key                 TEXT        NOT NULL,
    name                TEXT        NOT NULL,
    jira_project_key    TEXT,
    default_base_branch TEXT,
    worker_cpu          INTEGER     CHECK (worker_cpu IS NULL OR worker_cpu IN (1024, 2048, 4096, 8192, 16384)),
    worker_memory_mb    INTEGER     CHECK (worker_memory_mb IS NULL OR worker_memory_mb BETWEEN 2048 AND 122880),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, key)
);

INSERT INTO projects (tenant_id, key, name)
SELECT DISTINCT tenant_id, project_id, project_id
  FROM delivery_programs
 WHERE project_id IS NOT NULL AND btrim(project_id) <> ''
ON CONFLICT (tenant_id, key) DO NOTHING;

ALTER TABLE delivery_programs
    ADD COLUMN IF NOT EXISTS project_ref UUID REFERENCES projects(id) ON DELETE SET NULL;

UPDATE delivery_programs dp
   SET project_ref = p.id
  FROM projects p
 WHERE dp.project_ref IS NULL
   AND p.tenant_id = dp.tenant_id
   AND p.key = dp.project_id;

CREATE INDEX IF NOT EXISTS idx_delivery_programs_project_ref
    ON delivery_programs (project_ref)
    WHERE project_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS project_registries (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type            TEXT        NOT NULL CHECK (type IN ('npm', 'pypi', 'maven', 'docker')),
    url             TEXT        NOT NULL,
    scope           TEXT,
    username        TEXT,
    secret_arn      TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_tested_at  TIMESTAMPTZ,
    last_test_ok    BOOLEAN,
    last_test_error TEXT,
    UNIQUE (project_id, type, url)
);

CREATE INDEX IF NOT EXISTS idx_project_registries_project ON project_registries (project_id);

ALTER TABLE session_audit_log
    DROP CONSTRAINT IF EXISTS session_audit_log_action_type_check;

ALTER TABLE session_audit_log
    ADD CONSTRAINT session_audit_log_action_type_check
    CHECK (action_type IN (
        'user_message',
        'route_decision',
        'specialist_dispatch',
        'specialist_result',
        'tool_invocation',
        'tool_result',
        'escalation',
        'approval_request',
        'error',
        'synthesis',
        'workflow_task_created',
        'workflow_task_claimed',
        'workflow_task_succeeded',
        'workflow_task_failed',
        'workflow_task_blocked',
        'workflow_task_cancelled',
        'workflow_task_approved',
        'workflow_task_rejected',
        'workflow_task_retried',
        'workflow_task_timed_out',
        'workflow_task_runtime_recorded',
        'workflow_tasks_expanded',
        'workflow_plan_approved',
        'workflow_plan_rejected',
        'workflow_record_created',
        'workflow_task_wait_set',
        'workflow_task_wait_cleared',
        'workflow_task_external_resumed',
        'workflow_task_input_patched',
        'workflow_cancelled',
        'workflow_completed',
        'workflow_superseded',
        'delivery_program_created',
        'delivery_program_advanced',
        'delivery_program_artifacts_patched',
        'delivery_program_cancelled',
        'workflow_linked_to_program',
        'delivery_program_patched',
        'program_link_created',
        'program_link_deleted',
        'program_briefs_upserted',
        'program_child_attached',
        'program_external_ref_recorded',
        'workflow_decision_recorded',
        'workflow_gate_denied',
        'workflow_gate_expired',
        'governance_setting_changed',
        'workflow_wait_escalated',
        'workflow_wait_expired',
        'program_release_recorded',
        'program_compensation_recorded',
        'program_evidence_exported',
        'department_memory_promoted',
        'workflow_task_human_resumed',
        'workflow_task_approval_resumed',
        'workflow_failed',
        'program_visibility_changed',
        'program_access_granted',
        'program_access_revoked',
        'program_admin_access',
        'coding_job_created',
        'coding_job_started',
        'coding_job_finished',
        'coding_job_lost',
        'coding_job_cancelled',
        'project_created',
        'project_updated',
        'registry_added',
        'registry_updated',
        'registry_deleted',
        'registry_tested'
    ));

COMMIT;
