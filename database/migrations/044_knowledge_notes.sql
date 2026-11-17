-- 044_knowledge_notes.sql
-- Reviewed knowledge notes loaded into runs.
-- scope is a text check list (org, project) so a later migration can add team.
-- revision_of / base_version / promoted_from carry edit, retire and promote
-- proposals. Approving an edit or retire updates the target; it does not
-- publish the proposal row itself.

BEGIN;

CREATE TABLE IF NOT EXISTS knowledge_notes (
    id               UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id        TEXT        NOT NULL DEFAULT 'default',
    scope            TEXT        NOT NULL CHECK (scope IN ('org', 'project')),
    project_id       UUID        REFERENCES projects(id) ON DELETE CASCADE,
    kind             TEXT        NOT NULL CHECK (kind IN ('avoid', 'howto')),
    title            TEXT        NOT NULL,
    body             TEXT        NOT NULL,
    why              TEXT        NOT NULL DEFAULT '',
    applies_to       TEXT[]      NOT NULL DEFAULT '{}',
    exception_to     UUID        REFERENCES knowledge_notes(id) ON DELETE SET NULL,
    status           TEXT        NOT NULL DEFAULT 'proposed'
                         CHECK (status IN ('proposed', 'active', 'retired', 'rejected')),
    version          INTEGER     NOT NULL DEFAULT 1 CHECK (version >= 1),
    evidence         JSONB       NOT NULL DEFAULT '[]',
    source           TEXT        NOT NULL DEFAULT 'manual'
                         CHECK (source IN ('manual', 'review', 'agent')),
    proposal_action  TEXT        NOT NULL DEFAULT 'add'
                         CHECK (proposal_action IN ('add', 'edit', 'retire', 'promote')),
    revision_of      UUID        REFERENCES knowledge_notes(id) ON DELETE SET NULL,
    base_version     INTEGER,
    promoted_from    UUID        REFERENCES knowledge_notes(id) ON DELETE SET NULL,
    proposed_by      TEXT        NOT NULL DEFAULT '',
    approved_by      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at     TIMESTAMPTZ,
    use_count        INTEGER     NOT NULL DEFAULT 0,
    CHECK (
        (scope = 'org' AND project_id IS NULL AND exception_to IS NULL)
        OR (scope = 'project' AND project_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS knowledge_note_versions (
    note_id        UUID        NOT NULL REFERENCES knowledge_notes(id) ON DELETE CASCADE,
    version        INTEGER     NOT NULL,
    snapshot       JSONB       NOT NULL,
    changed_by     TEXT        NOT NULL DEFAULT '',
    change_reason  TEXT        NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (note_id, version)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_notes_lookup
    ON knowledge_notes (tenant_id, scope, project_id, status);

CREATE INDEX IF NOT EXISTS idx_knowledge_notes_proposed
    ON knowledge_notes (tenant_id, status)
    WHERE status = 'proposed';

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
        'knowledge_note_status_changed'
    ));

COMMIT;
