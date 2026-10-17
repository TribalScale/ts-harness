import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "database" / "migrations"
MIGRATE_SH = REPO_ROOT / "scripts" / "ops" / "migrate-db.sh"


class MigrationContractTests(unittest.TestCase):
    def test_tenant_isolation_migration_adds_tenant_id_columns(self):
        sql = (MIGRATIONS / "007_tenant_isolation.sql").read_text()
        for table in ("memories", "workflow_tasks", "workflow_plans", "session_audit_log"):
            self.assertIn(table, sql)
            self.assertIn("tenant_id", sql)

    def test_workflows_migration_creates_workflows_table(self):
        sql = (MIGRATIONS / "008_workflows.sql").read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS workflows", sql)
        self.assertIn("correlation_id", sql)

    def test_scim_migration_creates_users_and_groups(self):
        sql = (MIGRATIONS / "009_scim.sql").read_text()
        self.assertIn("scim_users", sql)
        self.assertIn("scim_groups", sql)

    def test_migration_order_doc_lists_all_files(self):
        doc = (REPO_ROOT / "database" / "MIGRATION_ORDER.md").read_text()
        for name in (
            "007_tenant_isolation.sql",
            "011_retry_backoff.sql",
            "019_delivery_programs.sql",
            "022_program_definition_pinning.sql",
        ):
            self.assertIn(name, doc)

    def test_migrate_db_sh_includes_023_and_024_after_022(self):
        script = MIGRATE_SH.read_text()
        pin = "022_program_definition_pinning.sql"
        project = "023_program_project_memory.sql"
        fts = "024_transcript_fts.sql"
        self.assertIn(project, script)
        self.assertIn(fts, script)
        self.assertLess(script.index(pin), script.index(project))
        self.assertLess(script.index(project), script.index(fts))

    def test_migrate_db_sh_lists_every_migration_in_order(self):
        script = MIGRATE_SH.read_text()
        files = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
        missing = [name for name in files if name not in script]
        self.assertEqual(missing, [])
        positions = [script.index(name) for name in files if name.startswith("02")]
        self.assertEqual(positions, sorted(positions))

    def test_program_department_migration_keeps_existing_audit_actions(self):
        sql = (MIGRATIONS / "030_program_department.sql").read_text()
        self.assertIn("department TEXT", sql)
        self.assertIn("'department_memory_promoted'", sql)
        self.assertIn("'program_evidence_exported'", sql)
        script = MIGRATE_SH.read_text()
        self.assertLess(script.index("029_runner_leader_lease.sql"), script.index("030_program_department.sql"))


if __name__ == "__main__":
    unittest.main()
