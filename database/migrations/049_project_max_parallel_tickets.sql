-- 049_project_max_parallel_tickets.sql
-- How many of a project's Jira tickets `implement all <PROJECT>` builds at once.
-- 1 keeps one ticket at a time; above 1 the ticket pool starts every ticket whose blockers
-- are done, up to this many (jira/ticket_pool.py). The host's CODING_MAX_RUNNING still caps
-- how many coding jobs run at the same moment; the rest queue.

BEGIN;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS max_parallel_tickets INTEGER NOT NULL DEFAULT 2;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projects_max_parallel_tickets_check') THEN
    ALTER TABLE projects ADD CONSTRAINT projects_max_parallel_tickets_check
      CHECK (max_parallel_tickets BETWEEN 1 AND 8);
  END IF;
END $$;

COMMIT;
