-- 029_runner_leader_lease.sql
-- One workflow-runner leader at a time. A second runner idles until the lease expires.

BEGIN;

CREATE TABLE IF NOT EXISTS runner_leader_lease (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    runner_id text NOT NULL,
    expires_at timestamptz NOT NULL
);

COMMIT;
