-- 027_program_releases.sql
-- One row per environment promotion of a merged SHA. The previous successful
-- row for (repo, environment) is the rollback target.

BEGIN;

CREATE TABLE IF NOT EXISTS program_releases (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       TEXT        NOT NULL,
    program_id      UUID        REFERENCES delivery_programs(id) ON DELETE SET NULL,
    workflow_id     UUID,
    repo            TEXT        NOT NULL,
    sha             TEXT        NOT NULL,
    environment     TEXT        NOT NULL,
    deployment_ref  TEXT,
    status          TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN
                      ('pending', 'in_progress', 'succeeded', 'failed')),
    evidence_url    TEXT,
    promoted_by     TEXT,
    decision_id     UUID,
    rollback_sha    TEXT,
    is_rollback     BOOLEAN     NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_program_releases_program
    ON program_releases (program_id, created_at)
    WHERE program_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_program_releases_last_success
    ON program_releases (tenant_id, repo, environment, completed_at DESC)
    WHERE status = 'succeeded';

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
        'program_compensation_recorded'
    ));

COMMIT;
