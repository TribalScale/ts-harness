-- 011_workflow_task_contracts.sql
-- Persist an immutable resolved stage contract snapshot and the originating
-- skeleton definition_version on every workflow task. Phase 1 of the Platform
-- Durability Upgrades: the runner executes task.contract rather than the latest
-- skeleton-by-type, so in-flight graphs are never rewritten in place.
-- Idempotent: safe to re-run.

BEGIN;

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS contract JSONB NOT NULL DEFAULT '{}';

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS definition_version TEXT;

COMMIT;
