-- 051_data_retention.sql
-- Data retention (the apply_data_retention system job, docs/data-handling.md): a marker for
-- coding jobs and workflow tasks whose large fields were trimmed, so each run only looks at rows
-- it has not trimmed yet, the programs whose uploaded design images were deleted (kept apart so
-- the program row and its updated_at stay as they were), and indexes for the age scans the job runs in small batches.

BEGIN;

ALTER TABLE coding_jobs ADD COLUMN IF NOT EXISTS retention_trimmed_at TIMESTAMPTZ;
ALTER TABLE workflow_tasks ADD COLUMN IF NOT EXISTS retention_trimmed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS program_upload_purges (
    program_id  UUID        PRIMARY KEY REFERENCES delivery_programs(id) ON DELETE CASCADE,
    purged_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coding_jobs_retention
    ON coding_jobs (finished_at)
    WHERE retention_trimmed_at IS NULL AND finished_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_tasks_untrimmed
    ON workflow_tasks (workflow_id)
    WHERE retention_trimmed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_workflows_finished
    ON workflows (completed_at)
    WHERE status IN ('succeeded', 'failed', 'cancelled', 'superseded');

CREATE INDEX IF NOT EXISTS idx_transcript_created
    ON runtime_transcript_messages (created_at);

CREATE INDEX IF NOT EXISTS idx_runtime_audit_events_occurred
    ON runtime_audit_events (occurred_at);

COMMIT;
