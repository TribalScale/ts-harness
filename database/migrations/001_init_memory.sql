-- 001_init_memory.sql
-- Full schema for the TS Harness memory system.
-- Requires: PostgreSQL 16+ with pgvector extension.
-- Idempotent: safe to re-run (IF NOT EXISTS / OR REPLACE).

BEGIN;

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- memories – the core knowledge store
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    type          TEXT        NOT NULL CHECK (type IN ('episodic', 'semantic', 'procedural', 'relational', 'directive')),
    content       TEXT        NOT NULL,
    -- Embedding width MUST stay in sync with ENTERPRISE_MEMORY_EMBEDDING_DIM (default 1536), gateway
    -- memorySearch / embedding provider output, and scripts/memory-db-init.sh verification. To change
    -- dimensions, follow docs/compatibility.md (dual-write / backfill — do not alter in place blindly).
    embedding     vector(1536),
    scope         TEXT        NOT NULL DEFAULT 'global',  -- global | team:<name> | project:<name> | user:<name>
    source        TEXT,       -- conversation_id, jira_ticket, document URL, etc.
    confidence    FLOAT       DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,  -- NULL = permanent
    tags          TEXT[]      DEFAULT '{}',
    metadata      JSONB       DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- memory_audit – append-only log of every memory lifecycle event
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_audit (
    id          UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    memory_id   UUID        REFERENCES memories(id) ON DELETE SET NULL,
    action      TEXT        NOT NULL CHECK (action IN ('created', 'accessed', 'updated', 'archived', 'deleted', 'expired')),
    actor       TEXT        NOT NULL,  -- agent name or user identifier
    reason      TEXT,
    details     JSONB       DEFAULT '{}',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- HNSW index for fast approximate-nearest-neighbor cosine similarity search
CREATE INDEX IF NOT EXISTS idx_memories_embedding    ON memories USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_memories_scope        ON memories (scope);
CREATE INDEX IF NOT EXISTS idx_memories_type         ON memories (type);
CREATE INDEX IF NOT EXISTS idx_memories_created_at   ON memories (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_expires_at   ON memories (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_tags         ON memories USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_memories_metadata     ON memories USING gin (metadata);

CREATE INDEX IF NOT EXISTS idx_memory_audit_memory_id   ON memory_audit (memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_audit_occurred_at ON memory_audit (occurred_at DESC);

-- ---------------------------------------------------------------------------
-- Trigger: auto-update updated_at on row modification
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_set_updated_at ON memories;
CREATE TRIGGER memories_set_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW
    EXECUTE FUNCTION trg_set_updated_at();

-- ---------------------------------------------------------------------------
-- Function: expire_memories()
-- Archives memories whose expires_at has passed by writing an audit record
-- and then deleting the expired row.  Intended to be called periodically
-- (e.g. via pg_cron or an application-level scheduler).
-- Returns the number of memories expired.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION expire_memories()
RETURNS INTEGER AS $$
DECLARE
    expired_count INTEGER;
BEGIN
    -- Write audit entries for every memory about to expire
    INSERT INTO memory_audit (memory_id, action, actor, reason, details)
    SELECT
        id,
        'expired',
        'system/expire_memories',
        'TTL reached',
        jsonb_build_object(
            'type',       type,
            'scope',      scope,
            'expires_at', expires_at,
            'content_preview', left(content, 200)
        )
    FROM memories
    WHERE expires_at IS NOT NULL
      AND expires_at <= now();

    GET DIAGNOSTICS expired_count = ROW_COUNT;

    -- Remove the expired memories
    DELETE FROM memories
    WHERE expires_at IS NOT NULL
      AND expires_at <= now();

    RETURN expired_count;
END;
$$ LANGUAGE plpgsql;

COMMIT;
