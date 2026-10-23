-- 031_teams_directory.sql
-- Teams created from ts-harness-team-* identity groups, and a people directory
-- for the program Share dialog and Slack identity linking.

BEGIN;

CREATE TABLE IF NOT EXISTS teams (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    slug         TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    display_name TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'groups',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, slug)
);

CREATE TABLE IF NOT EXISTS directory_users (
    tenant_id    TEXT NOT NULL DEFAULT 'default',
    subject      TEXT NOT NULL,
    email        TEXT,
    display_name TEXT,
    roles        JSONB NOT NULL DEFAULT '[]'::jsonb,
    source       TEXT NOT NULL DEFAULT 'seen_users',
    last_seen_at TIMESTAMPTZ,
    disabled_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject)
);

CREATE INDEX IF NOT EXISTS idx_directory_users_email
    ON directory_users (tenant_id, lower(email));

CREATE TABLE IF NOT EXISTS team_members (
    team_id   UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    subject   TEXT NOT NULL,
    role      TEXT NOT NULL CHECK (role IN ('member', 'approver', 'manager')),
    source    TEXT NOT NULL DEFAULT 'groups',
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, subject, role)
);

CREATE INDEX IF NOT EXISTS idx_team_members_subject
    ON team_members (tenant_id, subject);

COMMIT;
