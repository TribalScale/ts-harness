-- 040_llm_usage_budgets.sql
-- Metered LLM calls from the proxy, monthly rollups for team budgets, budget
-- raises by managers and admins, and per-model request buckets.

BEGIN;

CREATE TABLE IF NOT EXISTS llm_usage (
    id            BIGSERIAL     PRIMARY KEY,
    ts            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    tenant_id     TEXT          NOT NULL DEFAULT 'default',
    team_id       TEXT,
    project_id    TEXT,
    program_id    TEXT,
    workflow_id   TEXT,
    task_id       TEXT,
    actor         TEXT,
    provider      TEXT          NOT NULL,
    model         TEXT          NOT NULL,
    input_tokens  INTEGER       NOT NULL DEFAULT 0,
    output_tokens INTEGER       NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(14,6) NOT NULL DEFAULT 0,
    status        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_team_ts ON llm_usage (tenant_id, team_id, ts);
CREATE INDEX IF NOT EXISTS idx_llm_usage_program ON llm_usage (program_id) WHERE program_id IS NOT NULL;

CREATE OR REPLACE VIEW llm_usage_monthly AS
SELECT tenant_id,
       team_id,
       date_trunc('month', ts)::date AS month,
       provider,
       model,
       count(*)           AS calls,
       sum(input_tokens)  AS input_tokens,
       sum(output_tokens) AS output_tokens,
       sum(cost_usd)      AS cost_usd
  FROM llm_usage
 GROUP BY tenant_id, team_id, date_trunc('month', ts)::date, provider, model;

-- month NULL is a permanent change; otherwise it applies to that month only.
CREATE TABLE IF NOT EXISTS team_budget_overrides (
    id         UUID          PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id  TEXT          NOT NULL DEFAULT 'default',
    team_id    TEXT          NOT NULL,
    month      DATE          CHECK (month IS NULL OR month = date_trunc('month', month)::date),
    amount_usd NUMERIC(12,2) NOT NULL CHECK (amount_usd >= 0),
    raised_by  TEXT          NOT NULL,
    reason     TEXT          NOT NULL,
    created_at TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_team_budget_overrides_team
    ON team_budget_overrides (tenant_id, team_id, created_at DESC);

CREATE TABLE IF NOT EXISTS llm_rate_buckets (
    key        TEXT             PRIMARY KEY,
    tokens     DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ      NOT NULL DEFAULT now()
);

ALTER TABLE session_audit_log
    DROP CONSTRAINT IF EXISTS session_audit_log_action_type_check;

ALTER TABLE session_audit_log
    ADD CONSTRAINT session_audit_log_action_type_check
    CHECK (action_type IN (
        'user_message',
        'route_decision',
        'specialist_dispatch',
        'specialist_result',
        'tool_invocation',
        'tool_result',
        'escalation',
        'approval_request',
        'error',
        'synthesis',
        'workflow_task_created',
        'workflow_task_claimed',
        'workflow_task_succeeded',
        'workflow_task_failed',
        'workflow_task_blocked',
        'workflow_task_cancelled',
        'workflow_task_approved',
        'workflow_task_rejected',
        'workflow_task_retried',
        'workflow_task_timed_out',
        'workflow_task_runtime_recorded',
        'workflow_tasks_expanded',
        'workflow_plan_approved',
        'workflow_plan_rejected',
        'workflow_record_created',
        'workflow_task_wait_set',
        'workflow_task_wait_cleared',
        'workflow_task_external_resumed',
        'workflow_task_input_patched',
        'workflow_cancelled',
        'workflow_completed',
        'workflow_superseded',
        'delivery_program_created',
        'delivery_program_advanced',
        'delivery_program_artifacts_patched',
        'delivery_program_cancelled',
        'workflow_linked_to_program',
        'delivery_program_patched',
        'program_link_created',
        'program_link_deleted',
        'program_briefs_upserted',
        'program_child_attached',
        'program_external_ref_recorded',
        'workflow_decision_recorded',
        'workflow_gate_denied',
        'workflow_gate_expired',
        'governance_setting_changed',
        'workflow_wait_escalated',
        'workflow_wait_expired',
        'program_release_recorded',
        'program_compensation_recorded',
        'program_evidence_exported',
        'department_memory_promoted',
        'workflow_task_human_resumed',
        'workflow_task_approval_resumed',
        'workflow_failed',
        'program_visibility_changed',
        'program_access_granted',
        'program_access_revoked',
        'program_admin_access',
        'coding_job_created',
        'coding_job_started',
        'coding_job_finished',
        'coding_job_lost',
        'coding_job_cancelled',
        'project_created',
        'project_updated',
        'registry_added',
        'registry_updated',
        'registry_deleted',
        'registry_tested',
        'github_registration_updated',
        'github_installation_changed',
        'team_budget_raised'
    ));

COMMIT;
