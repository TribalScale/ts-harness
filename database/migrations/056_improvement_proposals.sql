-- 056_improvement_proposals.sql
-- The self-improvement loop's proposals and its offline replay cases.
--
-- improvement_proposals: one row per change the weekly failure-pattern digest proposed (a
-- knowledge note, a skill edit, a setting change, a fragment change, or an idea a person has to
-- design because it touches something off limits). It carries the evidence (ticket and workflow
-- references, counts), the metric it should move, the offline evaluations with their cost, the
-- canary's numbers and the promotion. Status lifecycle:
--   proposed -> evaluating -> canary -> awaiting_approval -> promoted | rolled_back | rejected
-- (see ts_harness.improvement.proposals.TRANSITIONS). One live experiment per target: at most one
-- proposal per (tenant, target) is in canary or awaiting_approval.
--
-- improvement_replay_cases: a delivered ticket's first push as the code review gate saw it (the
-- work order, the PR's changed files with their patches, the gate's inputs) and the gate's first
-- verdict and blocking findings as ground truth, for replaying a candidate pre-push reviewer.
-- Contains client code excerpts: pruned after 90 days by the improvement loop job.

BEGIN;

CREATE TABLE IF NOT EXISTS improvement_proposals (
    id              BIGSERIAL     PRIMARY KEY,
    tenant_id       TEXT          NOT NULL DEFAULT 'default',
    type            TEXT          NOT NULL
                    CHECK (type IN ('knowledge_note', 'skill_edit', 'setting_change', 'fragment_change', 'needs_design')),
    status          TEXT          NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'evaluating', 'canary', 'awaiting_approval',
                                      'promoted', 'rolled_back', 'rejected')),
    target          TEXT          NOT NULL DEFAULT '' CHECK (length(target) <= 300),
    title           TEXT          NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    summary         TEXT          NOT NULL DEFAULT '' CHECK (length(summary) <= 4000),
    pattern_key     TEXT          NOT NULL DEFAULT '' CHECK (length(pattern_key) <= 300),
    metric          TEXT          NOT NULL DEFAULT '' CHECK (length(metric) <= 300),
    expected_effect TEXT          NOT NULL DEFAULT '' CHECK (length(expected_effect) <= 1000),
    risk            TEXT          NOT NULL DEFAULT '' CHECK (length(risk) <= 1000),
    change          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    evidence        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    evaluation      JSONB,
    evaluations     JSONB         NOT NULL DEFAULT '[]'::jsonb,
    canary          JSONB,
    promotion       JSONB,
    history         JSONB         NOT NULL DEFAULT '[]'::jsonb,
    cost_usd        NUMERIC(12,4) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    digest_id       TEXT,
    created_by      TEXT,
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_improvement_proposals_status ON improvement_proposals (tenant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_improvement_proposals_pattern ON improvement_proposals (tenant_id, pattern_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_improvement_proposals_live_target
    ON improvement_proposals (tenant_id, target) WHERE status IN ('canary', 'awaiting_approval');

CREATE TABLE IF NOT EXISTS improvement_replay_cases (
    id              BIGSERIAL     PRIMARY KEY,
    tenant_id       TEXT          NOT NULL DEFAULT 'default',
    kind            TEXT          NOT NULL DEFAULT 'pre_push_review' CHECK (kind IN ('pre_push_review')),
    workflow_id     TEXT          NOT NULL,
    pr_url          TEXT          NOT NULL DEFAULT '',
    head_sha        TEXT          NOT NULL DEFAULT '',
    truth_verdict   TEXT          NOT NULL CHECK (truth_verdict IN ('block', 'pass')),
    truth_findings  JSONB         NOT NULL DEFAULT '[]'::jsonb,
    inputs          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    verdicts        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    gate_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, kind, workflow_id)
);

CREATE INDEX IF NOT EXISTS idx_improvement_replay_cases_recent ON improvement_replay_cases (tenant_id, kind, gate_at DESC);

COMMIT;
