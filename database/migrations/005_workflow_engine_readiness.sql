-- 005_workflow_engine_readiness.sql
-- Add workflow primitives that prepare for a future repo-owned workflow engine.
-- This migration intentionally does not add a runner, queue, or scheduler.

BEGIN;

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS workflow_type TEXT NOT NULL DEFAULT 'generic',
    ADD COLUMN IF NOT EXISTS external_ref TEXT,
    ADD COLUMN IF NOT EXISTS runtime_task_id TEXT,
    ADD COLUMN IF NOT EXISTS approval_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK (approval_status IN ('not_required', 'pending', 'approved', 'rejected')),
    ADD COLUMN IF NOT EXISTS approval_required BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS approved_by TEXT,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS max_retries INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_by TEXT,
    ADD COLUMN IF NOT EXISTS lock_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_wt_workflow_type
    ON workflow_tasks (workflow_type);

CREATE INDEX IF NOT EXISTS idx_wt_external_ref
    ON workflow_tasks (external_ref)
    WHERE external_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_wt_runtime_task_id
    ON workflow_tasks (runtime_task_id)
    WHERE runtime_task_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_wt_ready_claim
    ON workflow_tasks (status, agent_id, created_at)
    WHERE status = 'pending';

CREATE OR REPLACE FUNCTION claim_workflow_task(p_task_id UUID, p_agent_id TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    claimed_count INTEGER;
BEGIN
    UPDATE workflow_tasks
    SET status = 'running', started_at = now(), attempt = attempt + 1
    WHERE id = p_task_id
      AND status = 'pending'
      AND agent_id = p_agent_id
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
        'workflow_task_rejected'
    ));

COMMIT;
