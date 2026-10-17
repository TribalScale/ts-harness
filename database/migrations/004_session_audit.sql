-- 004_session_audit.sql
-- Compliance-grade session audit log for all agent actions.
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS session_audit_log (
    id            UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id    TEXT        NOT NULL,
    agent_id      TEXT        NOT NULL,
    action_type   TEXT        NOT NULL
                  CHECK (action_type IN (
                      'user_message', 'route_decision', 'specialist_dispatch',
                      'specialist_result', 'tool_invocation', 'tool_result',
                      'escalation', 'approval_request', 'error', 'synthesis'
                  )),
    channel       TEXT,
    user_id       TEXT,
    summary       TEXT        NOT NULL,
    details       JSONB       DEFAULT '{}',
    confidence    FLOAT,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sal_session_id  ON session_audit_log (session_id);
CREATE INDEX IF NOT EXISTS idx_sal_agent_id    ON session_audit_log (agent_id);
CREATE INDEX IF NOT EXISTS idx_sal_action_type ON session_audit_log (action_type);
CREATE INDEX IF NOT EXISTS idx_sal_occurred_at ON session_audit_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_sal_user_id     ON session_audit_log (user_id) WHERE user_id IS NOT NULL;

-- Retention: partition-ready. For high-volume deployments, convert to
-- range-partitioned by occurred_at (monthly). For now, a single table
-- with a cleanup function suffices.
CREATE OR REPLACE FUNCTION purge_old_audit_logs(retention_days INTEGER DEFAULT 90)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM session_audit_log
    WHERE occurred_at < now() - (retention_days || ' days')::interval;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

COMMIT;
