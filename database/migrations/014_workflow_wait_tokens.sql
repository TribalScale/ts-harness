-- 014_workflow_wait_tokens.sql
-- External wait/resume tokens for wait_external hook stages.

BEGIN;

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS wait_token TEXT,
    ADD COLUMN IF NOT EXISTS expected_event_type TEXT;

CREATE INDEX IF NOT EXISTS idx_wt_wait_token
    ON workflow_tasks (wait_token)
    WHERE wait_token IS NOT NULL;

COMMIT;
