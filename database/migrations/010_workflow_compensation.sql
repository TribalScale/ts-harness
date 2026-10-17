-- 010_workflow_compensation.sql
-- Compensation task linkage for workflow rollback.

BEGIN;

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS compensation_task_id UUID REFERENCES workflow_tasks(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_wt_compensation ON workflow_tasks (compensation_task_id)
    WHERE compensation_task_id IS NOT NULL;

COMMIT;
