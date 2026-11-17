-- 037_task_lanes.sql
-- Batch claims by lane with fair ordering. Each task carries a lane (fast,
-- agent, coding) set from its stage, and the team and requester of the program
-- it belongs to, so one busy person or team cannot starve the others.

BEGIN;

ALTER TABLE workflow_tasks
    ADD COLUMN IF NOT EXISTS lane TEXT NOT NULL DEFAULT 'agent',
    ADD COLUMN IF NOT EXISTS team_id UUID,
    ADD COLUMN IF NOT EXISTS requested_by TEXT;

ALTER TABLE workflow_tasks
    DROP CONSTRAINT IF EXISTS workflow_tasks_lane_check;

ALTER TABLE workflow_tasks
    ADD CONSTRAINT workflow_tasks_lane_check
    CHECK (lane IN ('fast', 'agent', 'coding'));

CREATE INDEX IF NOT EXISTS idx_wt_lane_ready
    ON workflow_tasks (status, lane, created_at);

CREATE INDEX IF NOT EXISTS idx_wt_running_requester
    ON workflow_tasks (requested_by)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_wt_running_team
    ON workflow_tasks (team_id)
    WHERE status = 'running';

-- The program a workflow belongs to; child workflows take their parent's.
CREATE OR REPLACE FUNCTION workflow_program_id(p_workflow_id UUID)
RETURNS UUID AS $$
    WITH RECURSIVE chain AS (
        SELECT id, parent_workflow_id, program_id, 0 AS depth
          FROM workflows
         WHERE id = p_workflow_id
        UNION ALL
        SELECT w.id, w.parent_workflow_id, w.program_id, chain.depth + 1
          FROM workflows w
          JOIN chain ON w.id = chain.parent_workflow_id
         WHERE chain.program_id IS NULL
           AND chain.depth < 10
    )
    SELECT program_id FROM chain WHERE program_id IS NOT NULL ORDER BY depth LIMIT 1;
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION workflow_tasks_fill_owner()
RETURNS trigger AS $$
DECLARE
    owner RECORD;
BEGIN
    IF NEW.requested_by IS NOT NULL AND NEW.team_id IS NOT NULL THEN
        RETURN NEW;
    END IF;
    SELECT COALESCE(p.owner_id, p.requester_actor_id) AS requested_by, pr.team_id
      INTO owner
      FROM delivery_programs p
      LEFT JOIN projects pr ON pr.tenant_id = p.tenant_id AND pr.key = p.project_id
     WHERE p.id = workflow_program_id(NEW.workflow_id);
    IF FOUND THEN
        NEW.requested_by := COALESCE(NEW.requested_by, owner.requested_by);
        NEW.team_id := COALESCE(NEW.team_id, owner.team_id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workflow_tasks_fill_owner ON workflow_tasks;
CREATE TRIGGER workflow_tasks_fill_owner
    BEFORE INSERT ON workflow_tasks
    FOR EACH ROW EXECUTE FUNCTION workflow_tasks_fill_owner();

-- Open work gets its owner now; approval gates move to the fast lane.
UPDATE workflow_tasks t
   SET requested_by = COALESCE(t.requested_by, o.requested_by),
       team_id = COALESCE(t.team_id, o.team_id)
  FROM (
        SELECT task.id, COALESCE(p.owner_id, p.requester_actor_id) AS requested_by, pr.team_id
          FROM workflow_tasks task
          JOIN delivery_programs p ON p.id = workflow_program_id(task.workflow_id)
          LEFT JOIN projects pr ON pr.tenant_id = p.tenant_id AND pr.key = p.project_id
         WHERE task.status IN ('pending', 'running', 'blocked')
       ) o
 WHERE t.id = o.id;

UPDATE workflow_tasks
   SET lane = 'fast'
 WHERE status IN ('pending', 'blocked')
   AND approval_required;

-- Claim up to p_limit ready tasks of one lane, at most one per workflow and
-- none for a workflow that already has a running task. Approved gates and
-- resumed or retried tasks go first, then the requester and team with the
-- fewest running tasks, then the oldest.
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
BEGIN
    IF p_limit IS NULL OR p_limit <= 0 THEN
        RETURN;
    END IF;
    FOR candidate IN
        SELECT t.id, t.workflow_id
          FROM workflow_tasks t
         WHERE t.status = 'pending'
           AND t.lane = p_lane
           AND t.attempt <= t.max_retries
           AND (t.approval_required = false OR t.approval_status = 'approved')
           AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())
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
               lock_expires_at = now() + make_interval(secs => p_lease_seconds)
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

CREATE OR REPLACE FUNCTION renew_workflow_task_lease(p_task_id UUID, p_runner TEXT, p_lease_seconds INTEGER)
RETURNS BOOLEAN AS $$
DECLARE
    renewed INTEGER;
BEGIN
    UPDATE workflow_tasks
       SET lock_expires_at = now() + make_interval(secs => p_lease_seconds)
     WHERE id = p_task_id
       AND status = 'running'
       AND locked_by = p_runner;
    GET DIAGNOSTICS renewed = ROW_COUNT;
    RETURN renewed > 0;
END;
$$ LANGUAGE plpgsql;

COMMIT;
