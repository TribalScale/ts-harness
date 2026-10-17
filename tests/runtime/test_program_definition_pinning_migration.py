"""Migration contract for program definition pinning on delivery_programs."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "database" / "migrations" / "022_program_definition_pinning.sql"
MIGRATE_SH = REPO_ROOT / "scripts" / "ops" / "migrate-db.sh"


class ProgramDefinitionPinningMigrationTests(unittest.TestCase):
    def test_migration_022_adds_pinning_columns(self):
        self.assertTrue(MIGRATION.is_file(), f"missing migration: {MIGRATION}")
        sql = MIGRATION.read_text()
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("delivery_programs", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS program_type TEXT", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS program_definition_version TEXT", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS program_definition_revision TEXT", sql)
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS program_definition_snapshot JSONB",
            sql,
        )
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS resolved_workflow_versions JSONB",
            sql,
        )
        # Legacy rows stay null; migration must not backfill snapshots.
        self.assertNotIn("UPDATE delivery_programs", sql.upper())
        self.assertNotIn("UPDATE\n    delivery_programs", sql)

    def test_migrate_db_sh_includes_pinning_migration(self):
        script = MIGRATE_SH.read_text()
        self.assertIn("022_program_definition_pinning.sql", script)
        # Avoid dual 020_* filenames; pinning runs after 021.
        self.assertNotIn("020_program_definition_pinning.sql", script)
        self.assertLess(
            script.index("021_workflow_program_link_audit_action.sql"),
            script.index("022_program_definition_pinning.sql"),
        )


if __name__ == "__main__":
    unittest.main()
