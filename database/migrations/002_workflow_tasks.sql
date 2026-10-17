-- 002_workflow_tasks.sql
-- Lightweight workflow state machine for multi-step orchestrated tasks.
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS workflow_tasks (
    id            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_id   UUID        NOT NULL,
    step_name     TEXT        NOT NULL,
    agent_id      TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'blocked', 'cancelled')),
    depends_on    UUID[],
    input         JSONB       DEFAULT '{}',
    output        JSONB       DEFAULT '{}',
    error         TEXT,
    attempt       INTEGER     DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_wt_workflow_id ON workflow_tasks (workflow_id);
CREATE INDEX IF NOT EXISTS idx_wt_status      ON workflow_tasks (status);
CREATE INDEX IF NOT EXISTS idx_wt_agent_id    ON workflow_tasks (agent_id);

-- Auto-update updated_at
DROP TRIGGER IF EXISTS workflow_tasks_set_updated_at ON workflow_tasks;
CREATE TRIGGER workflow_tasks_set_updated_at
    BEFORE UPDATE ON workflow_tasks
    FOR EACH ROW
    EXECUTE FUNCTION trg_set_updated_at();

-- Claim a task: atomically move pending->running (prevents double-dispatch).
CREATE OR REPLACE FUNCTION claim_workflow_task(p_task_id UUID, p_agent_id TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    claimed BOOLEAN;
BEGIN
    UPDATE workflow_tasks
    SET status = 'running', started_at = now(), attempt = attempt + 1
    WHERE id = p_task_id
      AND status = 'pending'
      AND agent_id = p_agent_id
      AND NOT EXISTS (
          SELECT 1 FROM workflow_tasks dep
          WHERE dep.id = ANY(workflow_tasks.depends_on)
            AND dep.status NOT IN ('succeeded')
      );
    GET DIAGNOSTICS claimed = ROW_COUNT;
    RETURN claimed > 0;
END;
$$ LANGUAGE plpgsql;

-- Check if all tasks in a workflow are terminal.
CREATE OR REPLACE FUNCTION is_workflow_complete(p_workflow_id UUID)
RETURNS BOOLEAN AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM workflow_tasks
        WHERE workflow_id = p_workflow_id
          AND status NOT IN ('succeeded', 'failed', 'cancelled')
    );
$$ LANGUAGE sql STABLE;

COMMIT;
