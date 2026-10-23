import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "scripts" / "memory-gateway.py"
MIGRATION = REPO_ROOT / "database" / "migrations" / "005_workflow_engine_readiness.sql"
MIGRATION_006 = REPO_ROOT / "database" / "migrations" / "006_workflow_runner.sql"
MIGRATION_016 = (
    REPO_ROOT / "database" / "migrations" / "016_workflow_wait_audit_actions.sql"
)
MIGRATION_017_FILE = "017_workflow_lifecycle_audit_actions.sql"
MIGRATION_017 = (
    REPO_ROOT / "database" / "migrations" / MIGRATION_017_FILE
)
MIGRATION_020 = (
    REPO_ROOT
    / "database"
    / "migrations"
    / "020_delivery_program_audit_actions.sql"
)
MIGRATION_021 = (
    REPO_ROOT
    / "database"
    / "migrations"
    / "021_workflow_program_link_audit_action.sql"
)
MIGRATION_023 = (
    REPO_ROOT
    / "database"
    / "migrations"
    / "023_program_project_memory.sql"
)
ARCH_DOC = REPO_ROOT / "docs" / "architecture" / "workflow-engine.md"


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway", GATEWAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_readiness_migration_adds_future_engine_fields(self):
        migration = MIGRATION.read_text()

        for column in (
            "workflow_type",
            "external_ref",
            "runtime_task_id",
            "approval_status",
            "approval_required",
            "approved_by",
            "approved_at",
            "max_retries",
            "locked_by",
            "lock_expires_at",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", migration)
        self.assertIn("idx_wt_ready_claim", migration)
        self.assertIn("CREATE OR REPLACE FUNCTION claim_workflow_task", migration)
        self.assertIn("claimed_count INTEGER", migration)
        self.assertIn("GET DIAGNOSTICS claimed_count = ROW_COUNT", migration)
        self.assertIn("RETURN claimed_count > 0", migration)
        self.assertIn("approval_required = false OR approval_status = 'approved'", migration)
        for action_type in (
            "workflow_task_created",
            "workflow_task_claimed",
            "workflow_task_succeeded",
            "workflow_task_failed",
            "workflow_task_blocked",
            "workflow_task_cancelled",
            "workflow_task_approved",
            "workflow_task_rejected",
        ):
            self.assertIn(action_type, migration)
        self.assertIn("BEGIN;", migration)
        self.assertIn("COMMIT;", migration)

    def test_workflow_runner_migration_adds_plans_leases_and_audit_actions(self):
        migration = MIGRATION_006.read_text()

        self.assertIn("CREATE TABLE IF NOT EXISTS workflow_plans", migration)
        self.assertIn("parent_task_id", migration)
        self.assertIn("UNIQUE", migration)
        self.assertIn("proposed_tasks", migration)
        self.assertIn("idx_wp_pending", migration)
        self.assertIn("WHERE approval_status = 'pending'", migration)
        self.assertIn("idx_wt_stale_running", migration)
        self.assertIn("attempt <= max_retries", migration)
        self.assertIn("p_locked_by TEXT DEFAULT NULL", migration)
        self.assertIn("p_lease_seconds INTEGER DEFAULT NULL", migration)
        self.assertIn(
            "DROP CONSTRAINT IF EXISTS session_audit_log_action_type_check",
            migration,
        )
        for action_type in (
            "workflow_task_retried",
            "workflow_task_timed_out",
            "workflow_task_runtime_recorded",
            "workflow_tasks_expanded",
            "workflow_plan_approved",
            "workflow_plan_rejected",
        ):
            self.assertIn(action_type, migration)

    def test_workflow_wait_migration_adds_external_wait_audit_actions(self):
        migration = MIGRATION_016.read_text()

        self.assertIn(
            "DROP CONSTRAINT IF EXISTS session_audit_log_action_type_check",
            migration,
        )
        for action_type in (
            "workflow_task_wait_set",
            "workflow_task_wait_cleared",
            "workflow_task_external_resumed",
        ):
            self.assertIn(action_type, migration)

    def test_workflow_lifecycle_migration_adds_memory_gateway_audit_actions(self):
        migration = MIGRATION_017.read_text()

        self.assertIn(
            "DROP CONSTRAINT IF EXISTS session_audit_log_action_type_check",
            migration,
        )
        for action_type in (
            "workflow_task_input_patched",
            "workflow_cancelled",
            "workflow_completed",
            "workflow_superseded",
        ):
            self.assertIn(action_type, migration)

    def test_delivery_program_migration_adds_audit_actions(self):
        migration = MIGRATION_020.read_text()

        self.assertIn(
            "DROP CONSTRAINT IF EXISTS session_audit_log_action_type_check",
            migration,
        )
        for action_type in (
            "delivery_program_created",
            "delivery_program_advanced",
            "delivery_program_artifacts_patched",
            "delivery_program_cancelled",
        ):
            self.assertIn(action_type, migration)

    def test_workflow_program_link_migration_adds_audit_action(self):
        migration = MIGRATION_021.read_text()

        self.assertIn(
            "DROP CONSTRAINT IF EXISTS session_audit_log_action_type_check",
            migration,
        )
        for action_type in (
            "delivery_program_created",
            "delivery_program_advanced",
            "delivery_program_artifacts_patched",
            "delivery_program_cancelled",
            "workflow_linked_to_program",
        ):
            self.assertIn(action_type, migration)

    def test_program_project_memory_migration_adds_tables_and_audit_actions(self):
        migration = MIGRATION_023.read_text()

        self.assertIn("CREATE TABLE IF NOT EXISTS program_links", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS program_briefs", migration)
        self.assertIn("excluded_from_autoshare", migration)
        for action_type in (
            "delivery_program_patched",
            "program_link_created",
            "program_link_deleted",
            "program_briefs_upserted",
        ):
            self.assertIn(action_type, migration)

    def test_workflow_status_transitions_are_explicit(self):
        gateway = load_gateway()

        self.assertTrue(gateway.can_transition("pending", "running"))
        self.assertTrue(gateway.can_transition("running", "succeeded"))
        self.assertTrue(gateway.can_transition("blocked", "pending"))
        self.assertTrue(gateway.can_transition("blocked", "blocked"))
        self.assertFalse(gateway.can_transition("pending", "succeeded"))
        self.assertFalse(gateway.can_transition("succeeded", "running"))
        self.assertFalse(gateway.can_transition("cancelled", "pending"))

    def test_approval_required_task_cannot_be_claimed_before_approval(self):
        gateway = load_gateway()
        task_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor(
            rows=[
                {
                    "id": task_id,
                    "approval_required": True,
                    "approval_status": "pending",
                }
            ]
        )
        conn = FakeConnection(cursor)

        with self.assertRaises(ValueError):
            gateway.claim_workflow_task(conn, {"id": task_id, "agent_id": "pm-agent"})

    def test_approved_task_can_be_claimed_and_audited(self):
        gateway = load_gateway()
        task_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor(
            rows=[
                {
                    "id": task_id,
                    "approval_required": True,
                    "approval_status": "approved",
                },
                {"claimed": True},
            ]
        )
        conn = FakeConnection(cursor)

        result = gateway.claim_workflow_task(
            conn,
            {"id": task_id, "agent_id": "pm-agent", "actor": "orchestrator"},
        )

        self.assertEqual(result, {"claimed": True, "id": task_id})
        self.assertTrue(conn.committed)
        executed_sql = "\n".join(sql for sql, _ in cursor.calls)
        executed_params = [param for _, params in cursor.calls for param in (params or ())]
        self.assertIn("claim_workflow_task", executed_sql)
        self.assertIn("SELECT claim_workflow_task(%s, %s)", executed_sql)
        self.assertIn("session_audit_log", executed_sql)
        self.assertIn("workflow_task_claimed", executed_params)

    def test_lease_claim_uses_owner_and_lease_arguments(self):
        gateway = load_gateway()
        task_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor(
            rows=[
                {
                    "id": task_id,
                    "approval_required": False,
                    "approval_status": "not_required",
                },
                {"claimed": True},
            ]
        )
        conn = FakeConnection(cursor)

        result = gateway.claim_workflow_task(
            conn,
            {
                "id": task_id,
                "agent_id": "pm-agent",
                "actor": "workflow-runner",
                "locked_by": "runner-1",
                "lease_seconds": 1800,
            },
        )

        self.assertEqual(result, {"claimed": True, "id": task_id})
        executed_sql = "\n".join(sql for sql, _ in cursor.calls)
        executed_params = [param for _, params in cursor.calls for param in (params or ())]
        self.assertIn("SELECT claim_workflow_task(%s, %s, %s, %s)", executed_sql)
        self.assertIn("runner-1", executed_params)
        self.assertIn(1800, executed_params)

    def test_ready_query_filters_dependencies_approval_and_retry_budget(self):
        gateway = load_gateway()
        cursor = FakeCursor(rows=[{"id": "task-1"}])
        conn = FakeConnection(cursor)

        result = gateway.list_ready_workflow_tasks(conn, {"limit": 5})

        self.assertEqual(result, [{"id": "task-1"}])
        executed_sql = " ".join(" ".join(sql.split()) for sql, _ in cursor.calls)
        self.assertIn("status = 'pending'", executed_sql)
        self.assertIn("attempt <= max_retries", executed_sql)
        self.assertIn("approval_required = false OR approval_status = 'approved'", executed_sql)
        self.assertIn("dep.status NOT IN ('succeeded')", executed_sql)

    def test_retry_sets_next_attempt_at_backoff(self):
        gateway = load_gateway()
        task_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        result = gateway.retry_workflow_task(
            conn,
            {"id": task_id, "reason": "transport_error: refused", "actor": "workflow-runner"},
        )

        self.assertEqual(result, {"id": task_id, "status": "pending"})
        self.assertTrue(conn.committed)
        executed_sql = " ".join(" ".join(sql.split()) for sql, _ in cursor.calls)
        self.assertIn("next_attempt_at", executed_sql)
        self.assertIn("LEAST(300, POWER(2, GREATEST(attempt, 1))::int)", executed_sql)
        self.assertIn("status = 'pending'", executed_sql)

    def test_resume_tool_approval_requeues_without_spending_retry(self):
        gateway = load_gateway()
        task_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        result = gateway.resume_tool_approval_task(
            conn, {"id": task_id, "approval_id": "ap-1", "actor": "admin-1"}
        )

        self.assertEqual(result, {"id": task_id, "status": "pending"})
        self.assertTrue(conn.committed)
        executed_sql = " ".join(" ".join(sql.split()) for sql, _ in cursor.calls)
        executed_params = [p for _, params in cursor.calls for p in (params or ())]
        self.assertIn("attempt = GREATEST(attempt - 1, 0)", executed_sql)
        self.assertIn("status = 'blocked'", executed_sql)
        self.assertIn("awaiting_tool_approval:ap-1", executed_params)
        self.assertNotIn("POWER(2", executed_sql)

    def test_runtime_task_recording_audits_prior_runtime_id(self):
        gateway = load_gateway()
        task_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor(rows=[{"runtime_task_id": "old-runtime"}])
        conn = FakeConnection(cursor)

        result = gateway.record_runtime_task(
            conn,
            {
                "id": task_id,
                "runtime_task_id": "new-runtime",
                "actor": "workflow-runner",
            },
        )

        self.assertEqual(result, {"id": task_id, "runtime_task_id": "new-runtime"})
        executed_sql = "\n".join(sql for sql, _ in cursor.calls)
        executed_params = [param for _, params in cursor.calls for param in (params or ())]
        self.assertIn("runtime_task_id", executed_sql)
        self.assertIn("session_audit_log", executed_sql)
        self.assertIn("workflow_task_runtime_recorded", executed_params)
        self.assertIn("old-runtime", " ".join(str(param) for param in executed_params))

    def test_workflow_plan_lifecycle_uses_dedicated_table(self):
        gateway = load_gateway()
        plan_id = "00000000-0000-4000-8000-000000000099"
        cursor = FakeCursor(
            rows=[
                {"id": plan_id},
                {
                    "workflow_id": "00000000-0000-4000-8000-000000000001",
                    "approval_status": "pending",
                    "proposed_tasks": [
                        {"step_name": "child", "agent_id": "pm-agent", "input": {}}
                    ],
                },
            ]
        )
        conn = FakeConnection(cursor)

        proposed = gateway.propose_workflow_plan(
            conn,
            {
                "workflow_id": "00000000-0000-4000-8000-000000000001",
                "parent_task_id": "00000000-0000-4000-8000-000000000002",
                "proposed_tasks": [{"step_name": "child", "agent_id": "pm-agent", "input": {}}],
                "actor": "workflow-runner",
            },
        )
        approved = gateway.approve_workflow_plan(
            conn,
            {"plan_id": plan_id, "actor": "human:tester"},
        )

        self.assertEqual(proposed, {"plan_id": plan_id})
        self.assertEqual(approved["plan_id"], plan_id)
        executed_sql = "\n".join(sql for sql, _ in cursor.calls)
        self.assertIn("INSERT INTO workflow_plans", executed_sql)
        self.assertIn("FOR UPDATE", executed_sql)
        self.assertIn("UPDATE workflow_plans", executed_sql)
        self.assertIn("materialized_at", executed_sql)

    def test_completion_update_enforces_approval_gate_in_sql(self):
        gateway = load_gateway()
        task_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor(
            rows=[
                {
                    "status": "running",
                    "approval_required": True,
                    "approval_status": "approved",
                }
            ]
        )
        conn = FakeConnection(cursor)

        gateway.transition_workflow_task(
            conn,
            {"id": task_id, "status": "succeeded", "actor": "orchestrator"},
        )

        update_sql = " ".join(
            " ".join(sql.split()) for sql, _ in cursor.calls
        )
        self.assertIn(
            "approval_required = false OR approval_status = 'approved'",
            update_sql,
        )

    def test_illegal_transition_is_rejected(self):
        gateway = load_gateway()
        cursor = FakeCursor(rows=[{"status": "pending"}])
        conn = FakeConnection(cursor)

        with self.assertRaises(ValueError):
            gateway.transition_workflow_task(
                conn,
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "status": "succeeded",
                },
            )

    def test_create_workflow_record_inserts_workflows_table(self):
        gateway = load_gateway()
        workflow_id = "00000000-0000-4000-8000-000000000010"
        cursor = FakeCursor(rows=[])
        conn = FakeConnection(cursor)

        result = gateway.create_workflow_record(
            conn,
            {
                "id": workflow_id,
                "workflow_type": "governed_delivery",
                "tenant_id": "tenant-a",
                "source_channel": "slack:C123:1234.5678",
                "actor": "orchestrator",
            },
        )

        self.assertEqual(result["id"], workflow_id)
        self.assertTrue(conn.committed)
        executed_sql = "\n".join(sql for sql, _ in cursor.calls)
        self.assertIn("INSERT INTO workflows", executed_sql)
        self.assertIn("tenant-a", str(cursor.calls))

    def test_create_workflow_task_includes_tenant_id(self):
        gateway = load_gateway()
        workflow_id = "00000000-0000-4000-8000-000000000011"
        cursor = FakeCursor(rows=[])
        conn = FakeConnection(cursor)

        gateway.create_workflow_task(
            conn,
            {
                "workflow_id": workflow_id,
                "step_name": "draft_prd",
                "agent_id": "pm-agent",
                "tenant_id": "tenant-a",
                "actor": "orchestrator",
            },
        )

        self.assertTrue(conn.committed)
        executed_sql = "\n".join(sql for sql, _ in cursor.calls)
        self.assertIn("tenant_id", executed_sql)
        params = [param for _, params in cursor.calls for param in (params or ())]
        self.assertIn("tenant-a", params)

    def test_architecture_doc_marks_runner_out_of_scope(self):
        doc = ARCH_DOC.read_text()

        self.assertIn("Current Phase", doc)
        self.assertIn("Future Phase", doc)
        self.assertIn("No background worker or queue", doc)
        self.assertIn("TS Harness AgentRuntime adapter", doc)


if __name__ == "__main__":
    unittest.main()
