-- 009_scim.sql
-- SCIM 2.0 user and group provisioning tables.

BEGIN;

CREATE TABLE IF NOT EXISTS scim_users (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       TEXT        NOT NULL,
    external_id     TEXT        NOT NULL,
    user_name       TEXT        NOT NULL,
    display_name    TEXT,
    email           TEXT,
    active          BOOLEAN     NOT NULL DEFAULT true,
    roles           TEXT[]      NOT NULL DEFAULT '{}',
    metadata        JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, external_id)
);

CREATE TABLE IF NOT EXISTS scim_groups (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       TEXT        NOT NULL,
    external_id     TEXT        NOT NULL,
    display_name    TEXT        NOT NULL,
    member_ids      UUID[]      NOT NULL DEFAULT '{}',
    metadata        JSONB       NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_scim_users_tenant ON scim_users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_scim_users_email ON scim_users (tenant_id, email);
CREATE INDEX IF NOT EXISTS idx_scim_groups_tenant ON scim_groups (tenant_id);

COMMIT;
