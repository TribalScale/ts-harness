BEGIN;

CREATE TABLE IF NOT EXISTS public.runtime_sessions (
    session_id TEXT PRIMARY KEY,
    routing_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    source TEXT NOT NULL,
    parent_session_id TEXT,
    subagent_id TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool_count INTEGER NOT NULL DEFAULT 0,
    compression_parent_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runtime_sessions_tenant ON public.runtime_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS idx_runtime_sessions_routing ON public.runtime_sessions (routing_key);

COMMIT;
