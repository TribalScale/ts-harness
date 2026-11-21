-- 048_project_revision_autonomy.sql
-- What the harness may decide on its own when a work order runs out of automatic revisions.
-- autonomous: continue while it makes progress, or accept with follow-up tickets for minor findings;
-- standard: continue while it makes progress, otherwise ask; conservative: always ask (the old behaviour).
-- Security-sensitive findings and the 8-revision ceiling always go to a person (workflows/revision_triage.py).

BEGIN;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS revision_autonomy TEXT NOT NULL DEFAULT 'autonomous';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projects_revision_autonomy_check') THEN
    ALTER TABLE projects ADD CONSTRAINT projects_revision_autonomy_check
      CHECK (revision_autonomy IN ('autonomous', 'standard', 'conservative'));
  END IF;
END $$;

COMMIT;
