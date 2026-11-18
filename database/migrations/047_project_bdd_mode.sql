-- 047_project_bdd_mode.sql
-- Whether a project's work is tested with Gherkin acceptance scenarios (BDD).
-- auto: on for repositories with no tests yet or that already use Gherkin; always; never.
-- Each program fixes the decision per repository when it starts (workflows/bdd_policy.py).

BEGIN;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS bdd_mode TEXT NOT NULL DEFAULT 'auto';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projects_bdd_mode_check') THEN
    ALTER TABLE projects ADD CONSTRAINT projects_bdd_mode_check CHECK (bdd_mode IN ('auto', 'always', 'never'));
  END IF;
END $$;

COMMIT;
