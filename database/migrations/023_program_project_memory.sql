-- 023_program_project_memory.sql
-- Project identity, directed program links, and approved-fact briefs.

BEGIN;

ALTER TABLE delivery_programs
    ADD COLUMN IF NOT EXISTS project_id TEXT,
    ADD COLUMN IF NOT EXISTS project_id_source TEXT
        CHECK (project_id_source IS NULL OR project_id_source IN
          ('inferred_jira', 'inferred_repo', 'user')),
    ADD COLUMN IF NOT EXISTS excluded_from_autoshare BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS program_links (
    id              UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       TEXT        NOT NULL,
    from_program_id UUID        NOT NULL REFERENCES delivery_programs(id) ON DELETE CASCADE,
    to_program_id   UUID        NOT NULL REFERENCES delivery_programs(id) ON DELETE CASCADE,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_program_id, to_program_id),
    CHECK (from_program_id <> to_program_id)
);

CREATE TABLE IF NOT EXISTS program_briefs (
    id                 UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id          TEXT        NOT NULL,
    project_id         TEXT,
    source_program_id  UUID        NOT NULL REFERENCES delivery_programs(id) ON DELETE CASCADE,
    phase              TEXT        NOT NULL,
    kind               TEXT        NOT NULL CHECK (kind IN ('decision', 'fact', 'directive', 'pointer')),
    topic              TEXT        NOT NULL,
    content            TEXT        NOT NULL,
    artifact_ref       TEXT,
    metadata           JSONB       NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_program_id, phase, kind, topic)
);

CREATE INDEX IF NOT EXISTS idx_delivery_programs_tenant_project
    ON delivery_programs (tenant_id, project_id)
    WHERE project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_program_briefs_tenant_project
    ON program_briefs (tenant_id, project_id)
    WHERE project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_program_briefs_source
    ON program_briefs (source_program_id);

CREATE INDEX IF NOT EXISTS idx_program_links_from
    ON program_links (from_program_id);

CREATE INDEX IF NOT EXISTS idx_program_links_to
    ON program_links (to_program_id);

DROP TRIGGER IF EXISTS program_briefs_set_updated_at ON program_briefs;
CREATE TRIGGER program_briefs_set_updated_at
    BEFORE UPDATE ON program_briefs
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
        'program_briefs_upserted'
    ));

COMMIT;
