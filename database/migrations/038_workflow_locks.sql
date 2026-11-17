-- 038_workflow_locks.sql
-- A short write lock per workflow so two runners or a runner and a webhook
-- never transition the same workflow at the same time. An expired lock is free.

BEGIN;

CREATE TABLE IF NOT EXISTS workflow_locks (
    workflow_id UUID        PRIMARY KEY,
    owner       TEXT        NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_locks_expires ON workflow_locks (expires_at);

COMMIT;
