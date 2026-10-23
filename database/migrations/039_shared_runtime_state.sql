-- 039_shared_runtime_state.sql
-- State that agent-runtime replicas share: tool approvals waiting for a person,
-- admin rate-limit windows, and leases for jobs only one replica may run.

BEGIN;

CREATE TABLE IF NOT EXISTS tool_approvals (
    approval_id         UUID        PRIMARY KEY,
    tenant_id           TEXT        NOT NULL,
    agent_id            TEXT        NOT NULL,
    tool_name           TEXT        NOT NULL,
    args                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    args_fingerprint    TEXT        NOT NULL,
    reason              TEXT        NOT NULL DEFAULT '',
    status              TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'denied')),
    requested_at        TEXT        NOT NULL,
    decided_by_actor_id TEXT,
    decided_by_roles    JSONB,
    consumed_at         TEXT,
    workflow_id         TEXT,
    task_id             TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_approvals_pending
    ON tool_approvals (tenant_id, requested_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tool_approvals_task
    ON tool_approvals (workflow_id, task_id) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS rate_limit_windows (
    key          TEXT        NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    hits         INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (key, window_start)
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_windows_start ON rate_limit_windows (window_start);

CREATE TABLE IF NOT EXISTS service_leases (
    name       TEXT        PRIMARY KEY,
    holder     TEXT        NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

COMMIT;
