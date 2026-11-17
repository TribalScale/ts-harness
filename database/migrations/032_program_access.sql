-- 032_program_access.sql
-- Private-by-default programs: an owner, a visibility and per-user or per-team
-- grants. Existing programs stay visible to everyone (public) and are owned by
-- their requester. Child programs take their access from the root program.
-- Also allows audit actions the memory API already writes but 030 omitted.

BEGIN;

ALTER TABLE delivery_programs
    ADD COLUMN IF NOT EXISTS owner_id TEXT,
    ADD COLUMN IF NOT EXISTS visibility TEXT;

UPDATE delivery_programs
   SET owner_id = COALESCE(owner_id, requester_actor_id),
       visibility = COALESCE(visibility, 'public')
 WHERE visibility IS NULL;

ALTER TABLE delivery_programs
    ALTER COLUMN visibility SET DEFAULT 'private',
    ALTER COLUMN visibility SET NOT NULL;

ALTER TABLE delivery_programs
    DROP CONSTRAINT IF EXISTS delivery_programs_visibility_check;

ALTER TABLE delivery_programs
    ADD CONSTRAINT delivery_programs_visibility_check
    CHECK (visibility IN ('private', 'public'));

CREATE INDEX IF NOT EXISTS idx_programs_owner
    ON delivery_programs (tenant_id, owner_id);

CREATE TABLE IF NOT EXISTS program_access (
    program_id     UUID NOT NULL REFERENCES delivery_programs(id) ON DELETE CASCADE,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'team')),
    principal_id   TEXT NOT NULL,
    level          TEXT NOT NULL CHECK (level IN ('read', 'write', 'approve', 'admin')),
    granted_by     TEXT,
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (program_id, principal_type, principal_id)
);

CREATE INDEX IF NOT EXISTS idx_program_access_principal
    ON program_access (principal_type, principal_id);

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
        'program_admin_access'
    ));

COMMIT;
