-- 025_program_hierarchy.sql
-- Parent/child delivery programs and a registry of external side effects
-- (Jira issues, PRs, branches) owned by each program.

BEGIN;

ALTER TABLE delivery_programs
    ADD COLUMN IF NOT EXISTS parent_program_id UUID
        REFERENCES delivery_programs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS root_program_id UUID
        REFERENCES delivery_programs(id) ON DELETE SET NULL;

ALTER TABLE delivery_programs
    DROP CONSTRAINT IF EXISTS delivery_programs_parent_not_self;

ALTER TABLE delivery_programs
    ADD CONSTRAINT delivery_programs_parent_not_self
    CHECK (parent_program_id IS NULL OR parent_program_id <> id);

CREATE INDEX IF NOT EXISTS idx_delivery_programs_parent
    ON delivery_programs (parent_program_id)
    WHERE parent_program_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_delivery_programs_root
    ON delivery_programs (root_program_id)
    WHERE root_program_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS program_external_refs (
    id           UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id    TEXT        NOT NULL,
    program_id   UUID        NOT NULL REFERENCES delivery_programs(id) ON DELETE CASCADE,
    workflow_id  UUID,
    kind         TEXT        NOT NULL CHECK (kind IN
                   ('jira_issue', 'pull_request', 'branch', 'deployment', 'confluence_page')),
    ref          TEXT        NOT NULL,
    state        TEXT        NOT NULL DEFAULT 'open' CHECK (state IN
                   ('open', 'merged', 'closed', 'deleted', 'labelled', 'failed')),
    metadata     JSONB       NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (program_id, kind, ref)
);

CREATE INDEX IF NOT EXISTS idx_program_external_refs_program
    ON program_external_refs (program_id);

CREATE INDEX IF NOT EXISTS idx_program_external_refs_tenant_kind_ref
    ON program_external_refs (tenant_id, kind, ref);

DROP TRIGGER IF EXISTS program_external_refs_set_updated_at ON program_external_refs;
CREATE TRIGGER program_external_refs_set_updated_at
    BEFORE UPDATE ON program_external_refs
    FOR EACH ROW
    EXECUTE FUNCTION trg_set_updated_at();

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
        'program_external_ref_recorded'
    ));

COMMIT;
