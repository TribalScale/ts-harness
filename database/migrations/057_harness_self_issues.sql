-- 057_harness_self_issues.sql
-- The harness-level self-improvement loop: one row per harness failure class (fingerprint) that
-- the harness filed, or will file, as an issue on its OWN repository, with how often it happened
-- and the state of its self-fix (a draft PR on the harness repository; a person always merges).
--
-- Only harness-side facts are stored: sanitised error signatures, harness file paths, harness
-- stack frames, counts and internal ids (workflow, task, coding job). Never client repository
-- names, ticket keys or text, code, design content or customer names: the sanitiser
-- (ts_harness.improvement.sanitize) runs before anything is written here.

BEGIN;

CREATE TABLE IF NOT EXISTS harness_self_issues (
    id                    UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id             TEXT          NOT NULL DEFAULT 'default',
    fingerprint           TEXT          NOT NULL CHECK (length(fingerprint) BETWEEN 8 AND 64),
    source                TEXT          NOT NULL CHECK (source IN ('arbiter', 'ops_alert')),
    kind                  TEXT          NOT NULL DEFAULT 'unknown'
                                        CHECK (kind IN ('code', 'configuration', 'operations', 'unknown')),
    component             TEXT          NOT NULL DEFAULT '',
    title                 TEXT          NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    signature             TEXT          NOT NULL DEFAULT '',
    diagnosis             TEXT          NOT NULL DEFAULT '',
    stack                 TEXT          NOT NULL DEFAULT '',
    files                 JSONB         NOT NULL DEFAULT '[]'::jsonb,
    occurrences           INTEGER       NOT NULL DEFAULT 0 CHECK (occurrences >= 0),
    reported_occurrences  INTEGER       NOT NULL DEFAULT 0 CHECK (reported_occurrences >= 0),
    seen_keys             JSONB         NOT NULL DEFAULT '[]'::jsonb,
    refs                  JSONB         NOT NULL DEFAULT '[]'::jsonb,
    first_seen            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    last_seen             TIMESTAMPTZ   NOT NULL DEFAULT now(),
    issue_repo            TEXT,
    issue_number          INTEGER,
    issue_url             TEXT,
    issue_state           TEXT,
    last_reported_at      TIMESTAMPTZ,
    fix_status            TEXT          NOT NULL DEFAULT 'none'
                                        CHECK (fix_status IN ('none', 'not_fixable', 'needs_human', 'started',
                                                              'pr_open', 'merged', 'closed', 'failed')),
    fix_reason            TEXT          NOT NULL DEFAULT '',
    fix_workflow_id       TEXT,
    fix_pr_url            TEXT,
    fix_pr_head           TEXT,
    fix_started_at        TIMESTAMPTZ,
    fix_charged_usd       NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (fix_charged_usd >= 0),
    last_error            TEXT          NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_harness_self_issues_seen ON harness_self_issues (tenant_id, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_harness_self_issues_fix ON harness_self_issues (tenant_id, fix_status, fix_started_at);

COMMIT;
