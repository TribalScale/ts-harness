-- 022_program_definition_pinning.sql
-- Persist immutable program definition topology pins on delivery program instances.
-- Legacy rows keep NULL snapshots and continue via frozen compatibility topology.

BEGIN;

ALTER TABLE delivery_programs
    ADD COLUMN IF NOT EXISTS program_type TEXT,
    ADD COLUMN IF NOT EXISTS program_definition_version TEXT,
    ADD COLUMN IF NOT EXISTS program_definition_revision TEXT,
    ADD COLUMN IF NOT EXISTS program_definition_snapshot JSONB,
    ADD COLUMN IF NOT EXISTS resolved_workflow_versions JSONB;

COMMIT;
