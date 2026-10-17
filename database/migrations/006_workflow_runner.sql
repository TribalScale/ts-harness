-- 006_workflow_runner.sql
-- Add workflow-runner support.
--
-- Retry semantics: max_retries is retries beyond the first attempt. The claim
-- function checks attempt <= max_retries before incrementing attempt, so the
-- existing default max_retries=0 still allows one attempt and no retries.
--
-- Plan storage: proposed child tasks are stored in workflow_plans, not inline on
-- workflow_tasks. A parent expand task records output.plan_id when it produces a
-- plan; the workflow_plans row owns approval and materialization state.
--
-- Runtime task id history is audit-only. record_runtime_task writes the prior
-- runtime_task_id into session_audit_log details before overwriting the field.
--
-- Rollback: DROP TABLE workflow_plans; DROP INDEX idx_wt_stale_running; restore
-- the 005 two-argument claim_workflow_task body and the prior audit constraint.
-- The stale index uses regular CREATE INDEX because current workflow_tasks
-- tables are small; switch to CREATE INDEX CONCURRENTLY outside a transaction
-- before promoting this migration to a high-row-count production database.

BEGIN;

CREATE TABLE IF NOT EXISTS workflow_plans (
    id                UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_id       UUID        NOT NULL,
    parent_task_id    UUID        NOT NULL UNIQUE REFERENCES workflow_tasks(id),
    proposed_tasks    JSONB       NOT NULL,
    approval_required BOOLEAN     NOT NULL DEFAULT true,
    approval_status   TEXT        NOT NULL DEFAULT 'pending'
        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_by       TEXT,
    approved_at       TIMESTAMPTZ,
    rejection_reason  TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    materialized_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_wp_workflow_id
    ON workflow_plans (workflow_id);

CREATE INDEX IF NOT EXISTS idx_wp_pending
    ON workflow_plans (approval_status)
    WHERE approval_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_wt_stale_running
    ON workflow_tasks (status, lock_expires_at)
    WHERE status = 'running';

CREATE OR REPLACE FUNCTION claim_workflow_task(
    p_task_id UUID,
    p_agent_id TEXT,
    p_locked_by TEXT DEFAULT NULL,
    p_lease_seconds INTEGER DEFAULT NULL
)
RETURNS BOOLEAN AS $$
DECLARE
    claimed_count INTEGER;
BEGIN
    UPDATE workflow_tasks
    SET status = 'running',
        started_at = now(),
        attempt = attempt + 1,
        locked_by = COALESCE(p_locked_by, locked_by),
        lock_expires_at = CASE
            WHEN p_lease_seconds IS NULL THEN lock_expires_at
            ELSE now() + (p_lease_seconds || ' seconds')::interval
        END
    WHERE id = p_task_id
      AND status = 'pending'
      AND agent_id = p_agent_id
      AND attempt <= max_retries
      AND (approval_required = false OR approval_status = 'approved')
      AND NOT EXISTS (
          SELECT 1 FROM workflow_tasks dep
          WHERE dep.id = ANY(workflow_tasks.depends_on)
            AND dep.status NOT IN ('succeeded')
      );
    GET DIAGNOSTICS claimed_count = ROW_COUNT;
    RETURN claimed_count > 0;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION claim_workflow_task(p_task_id UUID, p_agent_id TEXT)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN claim_workflow_task(p_task_id, p_agent_id, NULL, NULL);
END;
$$ LANGUAGE plpgsql;

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
        'workflow_plan_rejected'
    ));

COMMIT;
