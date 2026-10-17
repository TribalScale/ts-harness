-- 019_delivery_programs.sql
-- Federated delivery program coordinator.

BEGIN;

CREATE TABLE IF NOT EXISTS delivery_programs (
    id                  UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           TEXT        NOT NULL DEFAULT 'default',
    summary             TEXT        NOT NULL,
    display_name        TEXT,
    binding_key         TEXT,
    source_channel      TEXT,
    requester_actor_id  TEXT,
    entry_phase         TEXT        NOT NULL,
    current_phase       TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'blocked', 'failed', 'succeeded', 'cancelled')),
    metadata            JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE workflows
    ADD COLUMN IF NOT EXISTS program_id UUID REFERENCES delivery_programs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS program_phase TEXT;

CREATE INDEX IF NOT EXISTS idx_workflows_program
    ON workflows (program_id)
    WHERE program_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_programs_binding
    ON delivery_programs (tenant_id, binding_key)
    WHERE binding_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflows_parent
    ON workflows (parent_workflow_id)
    WHERE parent_workflow_id IS NOT NULL;

DROP TRIGGER IF EXISTS delivery_programs_set_updated_at ON delivery_programs;
CREATE TRIGGER delivery_programs_set_updated_at
    BEFORE UPDATE ON delivery_programs
    FOR EACH ROW
    EXECUTE FUNCTION trg_set_updated_at();

COMMIT;
