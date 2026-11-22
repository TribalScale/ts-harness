-- 051_program_budgets.sql
-- LLM budgets per delivery program. The runtime copies budgets.program_default_usd
-- and budgets.program_action into budget_settings; an admin or manager can set a
-- program's own amount (delivery_programs.budget_usd). Spend is the program's
-- total llm_usage. program_budget_alerts records which warnings were sent;
-- coding_jobs.held_reason says why a queued job waits, budget_note what the
-- ticket's thread is told (held, or run on the default model) and
-- budget_noted_at when it was told.

BEGIN;

ALTER TABLE budget_settings
    ADD COLUMN IF NOT EXISTS program_default_usd NUMERIC(12,2) NOT NULL DEFAULT 0
        CHECK (program_default_usd >= 0),
    ADD COLUMN IF NOT EXISTS program_action TEXT NOT NULL DEFAULT 'downgrade'
        CHECK (program_action IN ('downgrade', 'stop'));

ALTER TABLE delivery_programs
    ADD COLUMN IF NOT EXISTS budget_usd NUMERIC(12,2) CHECK (budget_usd IS NULL OR budget_usd >= 0),
    ADD COLUMN IF NOT EXISTS budget_set_by TEXT,
    ADD COLUMN IF NOT EXISTS budget_set_at TIMESTAMPTZ;

ALTER TABLE coding_jobs
    ADD COLUMN IF NOT EXISTS held_reason TEXT,
    ADD COLUMN IF NOT EXISTS budget_note TEXT,
    ADD COLUMN IF NOT EXISTS budget_noted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_coding_jobs_budget_note
    ON coding_jobs (created_at)
    WHERE budget_note IS NOT NULL AND budget_noted_at IS NULL;

CREATE TABLE IF NOT EXISTS program_budget_alerts (
    program_id UUID        NOT NULL REFERENCES delivery_programs(id) ON DELETE CASCADE,
    level      TEXT        NOT NULL CHECK (level IN ('warning', 'reached')),
    percent    NUMERIC(7,2),
    action     TEXT,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (program_id, level)
);

-- Usage recorded without its program (coding jobs whose program lookup failed).
UPDATE llm_usage u
   SET program_id = scoped.program_id
  FROM (
      SELECT id, workflow_program_id(workflow_uuid)::text AS program_id
        FROM (
            SELECT id,
                   CASE WHEN workflow_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        THEN workflow_id::uuid END AS workflow_uuid
              FROM llm_usage
             WHERE program_id IS NULL AND workflow_id IS NOT NULL
        ) candidates
       WHERE workflow_uuid IS NOT NULL
  ) scoped
 WHERE u.id = scoped.id AND scoped.program_id IS NOT NULL;

-- Effective budget: the program's own amount, then the tenant default; 0 means none.
CREATE OR REPLACE FUNCTION program_budget(p_program UUID)
RETURNS NUMERIC AS $$
    SELECT COALESCE(
        p.budget_usd,
        (SELECT s.program_default_usd FROM budget_settings s WHERE s.tenant_id = p.tenant_id),
        0
    )
      FROM delivery_programs p
     WHERE p.id = p_program;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION program_spend(p_program UUID)
RETURNS NUMERIC AS $$
    SELECT COALESCE(sum(u.cost_usd), 0) FROM llm_usage u WHERE u.program_id = p_program::text;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION program_budget_reached(p_program UUID)
RETURNS BOOLEAN AS $$
    SELECT COALESCE(program_budget(p_program), 0) > 0
       AND program_spend(p_program) >= program_budget(p_program);
$$ LANGUAGE sql STABLE;

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
        'team_budget_raised',
        'knowledge_note_created',
        'knowledge_note_updated',
        'knowledge_note_status_changed',
        'delivery_program_reopened',
        'delivery_program_closed_by_backfill',
        'program_budget_set'
    ));

COMMIT;
