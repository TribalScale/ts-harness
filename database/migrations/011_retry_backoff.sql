-- 011_retry_backoff.sql
-- Exponential retry backoff for workflow tasks.

BEGIN;

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;

ALTER TABLE workflow_plans
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_wt_next_attempt ON workflow_tasks (next_attempt_at)
    WHERE next_attempt_at IS NOT NULL;

COMMIT;
