-- 015_workflow_superseded.sql
-- Add explicit superseded workflow status and child workflow link.

BEGIN;

ALTER TABLE workflows DROP CONSTRAINT IF EXISTS workflows_status_check;
ALTER TABLE workflows ADD CONSTRAINT workflows_status_check
    CHECK (status IN ('running', 'succeeded', 'failed', 'blocked', 'cancelled', 'superseded'));

ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS superseded_by_workflow_id UUID REFERENCES workflows(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_workflows_superseded_by
    ON workflows (superseded_by_workflow_id)
    WHERE superseded_by_workflow_id IS NOT NULL;

COMMIT;
