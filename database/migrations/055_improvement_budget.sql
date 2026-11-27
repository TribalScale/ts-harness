-- 055_improvement_budget.sql
-- The self-improvement loop's model spend, apart from delivery budgets. One row per charge
-- (a digest, a proposal, a replay): its cost, what it was for and a reference (a digest id, a
-- proposal id). The cap is a setting (budgets.improvement_monthly_usd, default 150 USD) read by
-- the runtime; this ledger only sums a calendar month (UTC). No prompt or response text.

BEGIN;

CREATE TABLE IF NOT EXISTS improvement_spend (
    id            UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     TEXT          NOT NULL DEFAULT 'default',
    month         DATE          NOT NULL DEFAULT (date_trunc('month', now() AT TIME ZONE 'UTC'))::date,
    cost_usd      NUMERIC(12,4) NOT NULL CHECK (cost_usd >= 0),
    purpose       TEXT          NOT NULL CHECK (length(purpose) BETWEEN 1 AND 80),
    ref           TEXT,
    model         TEXT,
    input_tokens  INTEGER       CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER       CHECK (output_tokens IS NULL OR output_tokens >= 0),
    actor         TEXT,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_improvement_spend_month ON improvement_spend (tenant_id, month);

COMMIT;
