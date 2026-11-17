-- 033_coding_jobs.sql
-- Coding runs as queued jobs: the workflow runner parks the deliver task, a
-- launcher starts a worker container, and the worker reports back here.

BEGIN;

CREATE TABLE IF NOT EXISTS coding_jobs (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       TEXT        NOT NULL DEFAULT 'default',
    workflow_id     UUID        REFERENCES workflows(id) ON DELETE CASCADE,
    task_id         UUID        REFERENCES workflow_tasks(id) ON DELETE CASCADE,
    program_id      UUID,
    team_slug       TEXT,
    project_id      TEXT,
    requested_by    TEXT,
    repo_url        TEXT        NOT NULL,
    base_branch     TEXT,
    work_order_id   TEXT,
    spec            JSONB       NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'queued',
    launcher        TEXT,
    runner_ref      TEXT,
    attempt         INTEGER     NOT NULL DEFAULT 0,
    heartbeat_at    TIMESTAMPTZ,
    last_output     TEXT,
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at      TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    consumed_at     TIMESTAMPTZ,
    CONSTRAINT coding_jobs_status_check CHECK (status IN (
        'queued', 'starting', 'running', 'succeeded', 'failed', 'lost', 'cancelled'
    ))
);

CREATE INDEX IF NOT EXISTS idx_coding_jobs_status_created ON coding_jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_coding_jobs_task ON coding_jobs (task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_coding_jobs_workflow ON coding_jobs (workflow_id);

-- Named singleton leases for background services such as the coding launcher.
CREATE TABLE IF NOT EXISTS service_leases (
    name        TEXT        PRIMARY KEY,
    holder      TEXT        NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);

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
        'coding_job_cancelled'
    ));

COMMIT;
