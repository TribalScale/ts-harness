-- 007_tenant_isolation.sql
-- Add tenant_id to all multi-tenant tables.

BEGIN;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE memory_audit
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE workflow_plans
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE agent_token_usage
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE session_audit_log
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

-- Backfill memories from scope prefix tenant:X:...
UPDATE memories
SET tenant_id = split_part(scope, ':', 2)
WHERE scope LIKE 'tenant:%:%'
  AND tenant_id = 'default';

UPDATE memory_audit ma
SET tenant_id = m.tenant_id
FROM memories m
WHERE ma.memory_id = m.id
  AND ma.tenant_id = 'default';

CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories (tenant_id);
CREATE INDEX IF NOT EXISTS idx_wt_tenant_status ON workflow_tasks (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_wt_tenant_agent ON workflow_tasks (tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_wp_tenant ON workflow_plans (tenant_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_tenant ON agent_token_usage (tenant_id);
CREATE INDEX IF NOT EXISTS idx_session_audit_tenant ON session_audit_log (tenant_id);

COMMIT;
