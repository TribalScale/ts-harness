-- 043_shared_runtime_state.sql
-- State that used to live on the runtime host's disk, so any number of
-- runtime and runner processes on any machine share it:
--   shared_files       small file trees (workflow and other definitions, the
--                      cron file); each process mirrors them locally and
--                      pulls changes by namespace generation
--   skill_versions     index of managed skills; file content lives in object
--                      storage, approved versions under immutable prefixes
--   runtime_kv         small JSON state (scheduler last runs, delegation)
--   runtime_audit_events  the runtime audit log
--   channel_event_dedup   inbound channel events already handled

BEGIN;

CREATE TABLE IF NOT EXISTS shared_file_namespaces (
    namespace   TEXT        PRIMARY KEY,
    generation  BIGINT      NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shared_files (
    namespace   TEXT        NOT NULL,
    path        TEXT        NOT NULL,
    content     TEXT        NOT NULL DEFAULT '',
    sha256      TEXT        NOT NULL DEFAULT '',
    size        INTEGER     NOT NULL DEFAULT 0,
    generation  BIGINT      NOT NULL,
    deleted     BOOLEAN     NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT        NOT NULL DEFAULT '',
    PRIMARY KEY (namespace, path)
);

CREATE INDEX IF NOT EXISTS idx_shared_files_generation ON shared_files (namespace, generation);

CREATE TABLE IF NOT EXISTS shared_file_locks (
    namespace   TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    holder      TEXT        NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (namespace, name)
);

CREATE TABLE IF NOT EXISTS skill_versions (
    id            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     TEXT        NOT NULL DEFAULT 'default',
    skill_id      TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    category      TEXT        NOT NULL,
    leaf          TEXT        NOT NULL,
    layout        TEXT        NOT NULL DEFAULT 'folder' CHECK (layout IN ('file', 'folder')),
    status        TEXT        NOT NULL CHECK (status IN ('draft', 'active', 'superseded', 'rejected', 'approved')),
    object_prefix TEXT        NOT NULL,
    files         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    content_sha   TEXT        NOT NULL,
    source        TEXT        NOT NULL DEFAULT '',
    revision_of   TEXT,
    draft_id      UUID,
    created_by    TEXT        NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by    TEXT,
    decided_at    TIMESTAMPTZ,
    reason        TEXT
);

-- One catalog for the deployment: at most one live version and one open draft per skill id.
CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_versions_active
    ON skill_versions (skill_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS uq_skill_versions_draft
    ON skill_versions (skill_id) WHERE status = 'draft';
CREATE INDEX IF NOT EXISTS idx_skill_versions_status ON skill_versions (status, skill_id);

CREATE TABLE IF NOT EXISTS runtime_kv (
    namespace   TEXT        NOT NULL,
    key         TEXT        NOT NULL,
    value       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    version     BIGINT      NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS runtime_audit_events (
    sequence       BIGSERIAL   PRIMARY KEY,
    event_id       UUID        NOT NULL UNIQUE,
    tenant_id      TEXT        NOT NULL,
    action_type    TEXT        NOT NULL,
    actor_id       TEXT        NOT NULL,
    correlation_id TEXT,
    payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runtime_audit_events_tenant ON runtime_audit_events (tenant_id, occurred_at);

CREATE TABLE IF NOT EXISTS channel_event_dedup (
    key      TEXT        PRIMARY KEY,
    seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_channel_event_dedup_seen ON channel_event_dedup (seen_at);

COMMIT;
