-- 050_coding_job_progress.sql
-- Where a running coding job is: its current phase (setup, coding, verification, review, push),
-- how long it has been in it, and the phases it finished with their seconds. The worker sends it
-- with every heartbeat; the Live page shows it as the current run's breakdown.

BEGIN;

ALTER TABLE coding_jobs ADD COLUMN IF NOT EXISTS progress JSONB;

COMMIT;
