-- 053_coding_job_events.sql
-- File changes a coding run made, as they happened, for the console's live feed: one row per
-- file created, edited, deleted, renamed or restored (or one grouped row for many files changed
-- at once). The worker watches its checkout and sends them with its job token; excerpts are at
-- most 200 diff lines with secret-looking values masked before they leave the worker. Rows are
-- deleted after the coding-log retention period.

BEGIN;

CREATE TABLE IF NOT EXISTS coding_job_events (
    id            BIGSERIAL   PRIMARY KEY,
    job_id        UUID        NOT NULL REFERENCES coding_jobs(id) ON DELETE CASCADE,
    happened_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind          TEXT        NOT NULL
        CHECK (kind IN ('created', 'edited', 'deleted', 'renamed', 'restored', 'bulk')),
    path          TEXT        NOT NULL,
    from_path     TEXT,
    paths         JSONB,
    file_count    INTEGER     NOT NULL DEFAULT 1,
    bulk_kind     TEXT,
    additions     INTEGER     NOT NULL DEFAULT 0,
    deletions     INTEGER     NOT NULL DEFAULT 0,
    step          TEXT        NOT NULL DEFAULT '',
    actor         TEXT        NOT NULL DEFAULT 'agent' CHECK (actor IN ('agent', 'harness')),
    excerpt       TEXT        NOT NULL DEFAULT '',
    truncated     BOOLEAN     NOT NULL DEFAULT false,
    binary_file   BOOLEAN     NOT NULL DEFAULT false,
    masked        BOOLEAN     NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_coding_job_events_job ON coding_job_events (job_id, id);
CREATE INDEX IF NOT EXISTS idx_coding_job_events_created ON coding_job_events (created_at);
CREATE INDEX IF NOT EXISTS idx_coding_job_events_job_path ON coding_job_events (job_id, path);

COMMIT;
