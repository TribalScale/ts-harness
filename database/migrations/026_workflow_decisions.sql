-- 026_workflow_decisions.sql
-- Recorded human decisions (gates, track choices, promotions, governance
-- settings) and deadline columns for approval gates and external waits.

BEGIN;

CREATE TABLE IF NOT EXISTS workflow_decisions (
    id           UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id    TEXT        NOT NULL,
    program_id   UUID        REFERENCES delivery_programs(id) ON DELETE SET NULL,
    workflow_id  UUID,
    task_id      UUID,
    kind         TEXT        NOT NULL CHECK (kind IN
                   ('gate', 'track_choice', 'phase_entry', 'advance', 'promote', 'governance_setting')),
    decision     TEXT        NOT NULL,
    actor_id     TEXT        NOT NULL,
    actor_email  TEXT,
    actor_roles  JSONB       NOT NULL DEFAULT '[]',
    reason       TEXT,
    details      JSONB       NOT NULL DEFAULT '{}',
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflow_decisions_program
    ON workflow_decisions (program_id, created_at)
    WHERE program_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_decisions_workflow
    ON workflow_decisions (workflow_id, created_at)
    WHERE workflow_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_decisions_tenant_kind
    ON workflow_decisions (tenant_id, kind, created_at);

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS escalate_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS expires_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_workflow_tasks_deadlines
    ON workflow_tasks (escalate_at, expires_at)
    WHERE status IN ('pending', 'blocked') AND (escalate_at IS NOT NULL OR expires_at IS NOT NULL);

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
        'workflow_wait_expired'
    ));

COMMIT;
