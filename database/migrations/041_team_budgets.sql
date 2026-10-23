-- 041_team_budgets.sql
-- Monthly LLM budgets per team. The runtime copies the ``budgets`` config into
-- budget_settings; raises live in team_budget_overrides (migration 040). The
-- claim function holds agent and coding tasks of teams that reached their
-- budget, and team_budget_alerts records which warnings were sent.

BEGIN;

CREATE TABLE IF NOT EXISTS budget_settings (
    tenant_id            TEXT          PRIMARY KEY,
    currency             TEXT          NOT NULL DEFAULT 'USD',
    default_team_monthly NUMERIC(12,2) NOT NULL DEFAULT 5000 CHECK (default_team_monthly >= 0),
    warn_at_percent      INTEGER       NOT NULL DEFAULT 80 CHECK (warn_at_percent BETWEEN 1 AND 100),
    manager_max_monthly  NUMERIC(12,2) NOT NULL DEFAULT 10000 CHECK (manager_max_monthly >= 0),
    -- team slug or id -> monthly amount
    team_amounts         JSONB         NOT NULL DEFAULT '{}'::jsonb,
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS team_budget_alerts (
    tenant_id TEXT        NOT NULL,
    team_id   TEXT        NOT NULL,
    month     DATE        NOT NULL,
    level     TEXT        NOT NULL CHECK (level IN ('warning', 'reached')),
    percent   NUMERIC(7,2),
    sent_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, team_id, month, level)
);

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS held_reason TEXT;

-- Effective budget: this month's raise, then a permanent raise, then the team's
-- configured amount (by id or slug), then the tenant default, then 5,000.
CREATE OR REPLACE FUNCTION team_monthly_budget(p_tenant TEXT, p_team TEXT, p_month DATE)
RETURNS NUMERIC AS $$
    SELECT COALESCE(
        (SELECT o.amount_usd FROM team_budget_overrides o
          WHERE o.tenant_id = p_tenant AND o.team_id = p_team AND o.month = p_month
          ORDER BY o.created_at DESC LIMIT 1),
        (SELECT o.amount_usd FROM team_budget_overrides o
          WHERE o.tenant_id = p_tenant AND o.team_id = p_team AND o.month IS NULL
          ORDER BY o.created_at DESC LIMIT 1),
        (SELECT COALESCE(s.team_amounts ->> p_team,
                         s.team_amounts ->> (SELECT tm.slug FROM teams tm WHERE tm.id::text = p_team))::numeric
           FROM budget_settings s WHERE s.tenant_id = p_tenant),
        (SELECT s.default_team_monthly FROM budget_settings s WHERE s.tenant_id = p_tenant),
        5000
    );
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION team_month_spend(p_tenant TEXT, p_team TEXT, p_month DATE)
RETURNS NUMERIC AS $$
    SELECT COALESCE(sum(u.cost_usd), 0)
      FROM llm_usage u
     WHERE u.tenant_id = p_tenant
       AND u.team_id = p_team
       AND u.ts >= p_month
       AND u.ts < (p_month + interval '1 month');
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION team_budget_reached(p_tenant TEXT, p_team TEXT)
RETURNS BOOLEAN AS $$
    SELECT CASE
        WHEN p_team IS NULL THEN false
        ELSE team_month_spend(p_tenant, p_team, date_trunc('month', now())::date)
             >= team_monthly_budget(p_tenant, p_team, date_trunc('month', now())::date)
    END;
$$ LANGUAGE sql STABLE;

-- Same claim as migration 037, plus: agent and coding tasks of teams that
-- reached this month's budget stay pending with held_reason set.
CREATE OR REPLACE FUNCTION claim_ready_workflow_tasks(
    p_runner TEXT,
    p_lane TEXT,
    p_limit INTEGER,
    p_lease_seconds INTEGER
)
RETURNS SETOF workflow_tasks AS $$
DECLARE
    candidate RECORD;
    claimed workflow_tasks%ROWTYPE;
    taken INTEGER := 0;
    seen UUID[] := '{}';
    over_budget UUID[] := '{}';
BEGIN
    IF p_limit IS NULL OR p_limit <= 0 THEN
        RETURN;
    END IF;
    IF p_lane IN ('agent', 'coding') THEN
        over_budget := ARRAY(
            SELECT tm.id FROM teams tm WHERE team_budget_reached(tm.tenant_id, tm.id::text)
        );
        UPDATE workflow_tasks
           SET held_reason = 'team budget reached'
         WHERE status = 'pending' AND lane = p_lane AND team_id = ANY(over_budget)
           AND held_reason IS DISTINCT FROM 'team budget reached';
        UPDATE workflow_tasks
           SET held_reason = NULL
         WHERE status = 'pending' AND lane = p_lane AND held_reason = 'team budget reached'
           AND (team_id IS NULL OR NOT team_id = ANY(over_budget));
    END IF;
    FOR candidate IN
        SELECT t.id, t.workflow_id
          FROM workflow_tasks t
         WHERE t.status = 'pending'
           AND t.lane = p_lane
           AND t.attempt <= t.max_retries
           AND (t.approval_required = false OR t.approval_status = 'approved')
           AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())
           AND (t.team_id IS NULL OR NOT t.team_id = ANY(over_budget))
           AND NOT EXISTS (
               SELECT 1 FROM workflow_tasks dep
                WHERE dep.id = ANY(t.depends_on)
                  AND dep.status <> 'succeeded'
           )
         ORDER BY
           CASE
               WHEN (t.approval_required AND t.approval_status = 'approved') OR t.next_attempt_at IS NOT NULL THEN 0
               ELSE 1
           END,
           (SELECT count(*) FROM workflow_tasks r
             WHERE r.status = 'running' AND t.requested_by IS NOT NULL AND r.requested_by = t.requested_by),
           (SELECT count(*) FROM workflow_tasks r
             WHERE r.status = 'running' AND t.team_id IS NOT NULL AND r.team_id = t.team_id),
           t.created_at
         LIMIT p_limit * 4
         FOR UPDATE OF t SKIP LOCKED
    LOOP
        EXIT WHEN taken >= p_limit;
        CONTINUE WHEN candidate.workflow_id IS NOT NULL AND candidate.workflow_id = ANY(seen);
        IF candidate.workflow_id IS NOT NULL THEN
            CONTINUE WHEN NOT pg_try_advisory_xact_lock(hashtext('workflow_claim:' || candidate.workflow_id::text));
            CONTINUE WHEN EXISTS (
                SELECT 1 FROM workflow_tasks r
                 WHERE r.workflow_id = candidate.workflow_id AND r.status = 'running'
            );
        END IF;
        UPDATE workflow_tasks
           SET status = 'running',
               started_at = now(),
               attempt = attempt + 1,
               locked_by = p_runner,
               lock_expires_at = now() + make_interval(secs => p_lease_seconds),
               held_reason = NULL
         WHERE id = candidate.id
           AND status = 'pending'
        RETURNING * INTO claimed;
        IF FOUND THEN
            IF candidate.workflow_id IS NOT NULL THEN
                seen := seen || candidate.workflow_id;
            END IF;
            taken := taken + 1;
            RETURN NEXT claimed;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

COMMIT;
