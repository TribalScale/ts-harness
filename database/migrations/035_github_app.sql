-- GitHub App across organizations: one App registration per GitHub host, the
-- installations that App has (kept current by installation webhooks), and the
-- webhook delivery ids already processed so redeliveries are dropped.

BEGIN;

CREATE TABLE IF NOT EXISTS github_app_registrations (
    host                    TEXT PRIMARY KEY CHECK (host ~ '^[a-z0-9.-]+(:[0-9]+)?$'),
    app_id                  TEXT NOT NULL,
    app_slug                TEXT,
    private_key_secret_arn  TEXT NOT NULL,
    webhook_secret_arn      TEXT,
    created_by              TEXT NOT NULL DEFAULT 'admin',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS github_installations (
    host            TEXT NOT NULL,
    installation_id BIGINT NOT NULL,
    account_login   TEXT NOT NULL,
    account_type    TEXT NOT NULL DEFAULT 'Organization',
    repository_selection TEXT NOT NULL DEFAULT 'selected',
    suspended_at    TIMESTAMPTZ,
    repositories    JSONB NOT NULL DEFAULT '[]'::jsonb,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (host, installation_id)
);

CREATE INDEX IF NOT EXISTS idx_github_installations_account ON github_installations (host, lower(account_login));

CREATE TABLE IF NOT EXISTS github_webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    host        TEXT NOT NULL DEFAULT 'github.com',
    event       TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_github_webhook_deliveries_received ON github_webhook_deliveries (received_at);

-- GitHub login per person, from the identity source, for Co-authored-by trailers.
ALTER TABLE directory_users ADD COLUMN IF NOT EXISTS github_login TEXT;

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
        'github_installation_changed'
    ));

COMMIT;
