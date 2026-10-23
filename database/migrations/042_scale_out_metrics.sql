-- 042_scale_out_metrics.sql
-- Inputs for the scale-out dashboard and alarms: the last GitHub rate limit
-- seen for each App installation (written by the github-rate-limits system
-- job), and indexes for the per-window coding job and gate wait queries.

BEGIN;

ALTER TABLE github_installations
    ADD COLUMN IF NOT EXISTS rate_limit_remaining   INTEGER,
    ADD COLUMN IF NOT EXISTS rate_limit_limit       INTEGER,
    ADD COLUMN IF NOT EXISTS rate_limit_reset_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rate_limit_observed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_coding_jobs_finished
    ON coding_jobs (finished_at)
    WHERE finished_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_decisions_gate_created
    ON workflow_decisions (created_at)
    WHERE kind = 'gate';

COMMIT;
