-- 003_cost_tracking.sql
-- Per-agent LLM token usage tracking for cost visibility.
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_token_usage (
    id            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id      TEXT        NOT NULL,
    provider      TEXT        NOT NULL,
    model         TEXT        NOT NULL,
    input_tokens  INTEGER     NOT NULL DEFAULT 0,
    output_tokens INTEGER     NOT NULL DEFAULT 0,
    session_id    TEXT,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_atu_agent_id    ON agent_token_usage (agent_id);
CREATE INDEX IF NOT EXISTS idx_atu_recorded_at ON agent_token_usage (recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_atu_provider    ON agent_token_usage (provider, model);

-- Daily summary view for cost dashboards.
CREATE OR REPLACE VIEW daily_token_summary AS
SELECT
    date_trunc('day', recorded_at) AS day,
    agent_id,
    provider,
    model,
    SUM(input_tokens)  AS total_input_tokens,
    SUM(output_tokens) AS total_output_tokens,
    COUNT(*)           AS request_count
FROM agent_token_usage
GROUP BY 1, 2, 3, 4
ORDER BY 1 DESC, 5 DESC;

COMMIT;
