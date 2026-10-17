-- 008_workflows.sql
-- First-class workflow records.

BEGIN;

CREATE TABLE IF NOT EXISTS workflows (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           TEXT        NOT NULL DEFAULT 'default',
    workflow_type       TEXT        NOT NULL,
    definition_version  TEXT        NOT NULL DEFAULT '1',
    source_channel      TEXT,
    requester_actor_id  TEXT,
    status              TEXT        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'blocked', 'cancelled')),
    parent_workflow_id  UUID        REFERENCES workflows(id) ON DELETE SET NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    cancellation_reason TEXT,
    correlation_id      TEXT,
    compensation_status TEXT        DEFAULT 'none'
        CHECK (compensation_status IN ('none', 'pending', 'running', 'succeeded', 'failed')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflows_tenant ON workflows (tenant_id);
CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_workflows_correlation ON workflows (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- Link tasks to workflow records (workflow_id already exists on workflow_tasks)
CREATE INDEX IF NOT EXISTS idx_wt_workflow_fk ON workflow_tasks (workflow_id, tenant_id);

DROP TRIGGER IF EXISTS workflows_set_updated_at ON workflows;
CREATE TRIGGER workflows_set_updated_at
    BEFORE UPDATE ON workflows
    FOR EACH ROW
    EXECUTE FUNCTION trg_set_updated_at();

COMMIT;
