-- 046_llm_usage_agent.sql
-- The agent that made each model call. `actor` stays the person the work is for
-- (budgets and spend reports by user); for a workflow task that is the requester,
-- not the agent that ran the step. Coding-backend runs record `coding-backend`.

BEGIN;

ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS agent_id TEXT;

CREATE INDEX IF NOT EXISTS idx_llm_usage_agent_ts ON llm_usage (tenant_id, agent_id, ts);

COMMIT;
