import importlib.util
import os
import unittest
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "scripts" / "memory-gateway.py"


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway", GATEWAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

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


class MemoryGatewayTests(unittest.TestCase):
    def test_validate_memory_type_rejects_unknown_types(self):
        gateway = load_gateway()

        self.assertEqual(gateway.validate_memory_type("semantic"), "semantic")
        with self.assertRaises(ValueError):
            gateway.validate_memory_type("admin")

    def test_parse_limit_bounds_result_size(self):
        gateway = load_gateway()

        self.assertEqual(gateway.parse_limit(None), 10)
        self.assertEqual(gateway.parse_limit(5), 5)
        for value in (0, -1, 51, "many"):
            with self.assertRaises(ValueError):
                gateway.parse_limit(value)

    def test_parse_uuid_rejects_invalid_ids(self):
        gateway = load_gateway()

        valid = "00000000-0000-4000-8000-000000000000"
        self.assertEqual(gateway.parse_uuid(valid), valid)
        with self.assertRaises(ValueError):
            gateway.parse_uuid("not-a-uuid")

    def test_embedding_sql_literal_is_normalized_to_vector_json(self):
        gateway = load_gateway()

        self.assertEqual(
            gateway.normalize_embedding("'[0.1,-0.2]'::vector(1536)"),
            "[0.1,-0.2]",
        )
        self.assertEqual(
            gateway.normalize_embedding("[0.1,-0.2]"),
            "[0.1,-0.2]",
        )
        with self.assertRaises(ValueError):
            gateway.normalize_embedding("null")

    def test_remember_uses_parameterized_sql(self):
        gateway = load_gateway()
        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        gateway.remember(
            conn,
            {
                "type": "semantic",
                "content": "A memory with 'quotes' and ; SQL-looking text",
                "scope": "tenant:default:agent:orchestrator",
                "source": "test",
                "confidence": 0.9,
                "tags": ["safe"],
                "metadata": {"ticket": "PROJ-1"},
            },
            embed=lambda text: "[0.1,0.2]",
        )

        self.assertTrue(conn.committed)
        self.assertGreaterEqual(len(cursor.calls), 2)
        for sql, params in cursor.calls:
            self.assertIsNotNone(params)
            self.assertNotIn("A memory with 'quotes'", sql)
            self.assertNotIn("SQL-looking", sql)
        self.assertIn("%s::vector", "\n".join(sql for sql, _ in cursor.calls))
        insert_sql = next(sql for sql, _ in cursor.calls if "INSERT INTO memories" in sql)
        self.assertIn("tenant_id", insert_sql)

    def test_remember_existing_lookup_is_tenant_scoped(self):
        gateway = load_gateway()
        existing_id = "00000000-0000-4000-8000-000000000001"
        cursor = FakeCursor(rows=[{"id": existing_id}])
        conn = FakeConnection(cursor)
        gateway.remember(
            conn,
            {
                "type": "semantic",
                "content": "Program brief summary",
                "scope": "project:PROJ",
                "tenant_id": "tenant-a",
                "source": "test",
            },
            embed=lambda text: "[0.1,0.2]",
        )
        select_sql, select_params = next(
            (sql, params) for sql, params in cursor.calls if "SELECT id FROM memories" in sql
        )
        self.assertIn("tenant_id = %s", select_sql)
        self.assertIn("tenant-a", select_params)

    def test_recall_filters_tenant_and_scope(self):
        gateway = load_gateway()
        cursor = FakeCursor(rows=[])
        conn = FakeConnection(cursor)
        gateway.recall(
            conn,
            {
                "query": "approved prd",
                "tenant_id": "tenant-a",
                "scope": "project:PROJ",
                "limit": 5,
            },
            embed=lambda text: "[0.1,0.2]",
        )
        sql, params = cursor.calls[0]
        self.assertIn("tenant_id = %s", sql)
        self.assertIn("scope = %s OR scope = 'global'", sql)
        self.assertIn("tenant-a", params)
        self.assertIn("project:PROJ", params)

    def test_remember_rejects_global_and_department_scopes(self):
        gateway = load_gateway()
        conn = FakeConnection(FakeCursor())
        for scope in ("global", "Global", "department:engineering", "Department:engineering"):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(ValueError, "scope is closed"):
                    gateway.remember(
                        conn,
                        {"type": "semantic", "content": "a fact", "scope": scope},
                        embed=lambda text: "[0.1,0.2]",
                    )
        self.assertFalse(conn.committed)

    def test_create_program_ignores_department_on_the_body(self):
        gateway = load_gateway()
        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        result = gateway.create_delivery_program(
            conn,
            {
                "summary": "Project A",
                "entry_phase": "prd",
                "department": "sales",
            },
        )
        self.assertIsNone(result["department"])
        _sql, params = next(
            (sql, params) for sql, params in cursor.calls if "INSERT INTO delivery_programs" in sql
        )
        self.assertNotIn("sales", params)

        result = gateway.create_delivery_program(
            conn,
            {"summary": "Project A", "entry_phase": "prd", "department": "sales"},
            department="engineering",
        )
        self.assertEqual(result["department"], "engineering")

    def test_recall_hides_department_rows_unless_the_program_scope_is_set(self):
        gateway = load_gateway()
        cursor = FakeCursor(rows=[])
        conn = FakeConnection(cursor)
        gateway.recall(conn, {"query": "stack", "tenant_id": "default"}, embed=lambda text: "[0.1,0.2]")
        sql, _params = cursor.calls[0]
        self.assertIn("scope NOT ILIKE 'department:%'", sql)

        cursor.calls.clear()
        gateway.recall(
            conn,
            {
                "query": "stack",
                "tenant_id": "default",
                "scope": "tenant:default:agent:orchestrator",
                "department_scope": "department:engineering",
            },
            embed=lambda text: "[0.1,0.2]",
        )
        sql, params = cursor.calls[0]
        self.assertIn("scope = %s OR scope = 'global' OR scope = %s", sql)
        self.assertIn("department:engineering", params)
        self.assertNotIn("department:sales", params)

        with self.assertRaisesRegex(ValueError, "scope is closed"):
            gateway.recall(
                conn,
                {"query": "stack", "scope": "department:sales"},
                embed=lambda text: "[0.1,0.2]",
            )

    def test_promote_department_memory_updates_the_same_brief_row(self):
        gateway = load_gateway()
        program_id = "00000000-0000-4000-8000-000000000010"
        brief_id = "00000000-0000-4000-8000-000000000011"
        program = {
            "id": program_id,
            "tenant_id": "default",
            "department": "engineering",
        }
        brief = {
            "id": brief_id,
            "phase": "prd",
            "kind": "decision",
            "topic": "stack",
            "content": "Use Postgres",
        }

        class RoutingCursor(FakeCursor):
            def __init__(self, memory_row):
                super().__init__()
                self.memory_row = memory_row
                self.row = None

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                if "FROM delivery_programs" in sql:
                    self.row = program
                elif "FROM program_briefs" in sql:
                    self.row = brief
                elif "metadata->>'brief_id'" in sql:
                    self.row = self.memory_row
                else:
                    self.row = None

            def fetchone(self):
                return self.row

        created = RoutingCursor(None)
        first = gateway.promote_department_memory(
            FakeConnection(created),
            program_id,
            {"brief_id": brief_id, "tenant_id": "default", "actor": "ada"},
            embed=lambda text: "[0.1,0.2]",
        )
        self.assertEqual(first["action"], "created")
        self.assertEqual(first["scope"], "department:engineering")
        insert_sql, insert_params = next(
            (sql, params) for sql, params in created.calls if "INSERT INTO memories" in sql
        )
        self.assertIn("episodic", insert_params)
        self.assertIn("[0.1,0.2]", insert_params)
        self.assertTrue(
            any(params and "department_memory_promoted" in params for _sql, params in created.calls)
        )

        updated = RoutingCursor({"id": first["id"]})
        second = gateway.promote_department_memory(
            FakeConnection(updated),
            program_id,
            {"brief_id": brief_id, "tenant_id": "default", "actor": "ada"},
            embed=lambda text: "[0.3,0.4]",
        )
        self.assertEqual(second["action"], "updated")
        self.assertTrue(any("UPDATE memories" in sql for sql, _ in updated.calls))
        self.assertFalse(any("INSERT INTO memories" in sql for sql, _ in updated.calls))

    def test_complete_workflow_marks_superseded_with_link(self):
        gateway = load_gateway()
        workflow_id = "00000000-0000-4000-8000-000000000001"
        child_id = "00000000-0000-4000-8000-000000000002"
        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        result = gateway.complete_workflow_record(
            conn,
            workflow_id,
            {
                "actor": "orchestrator",
                "reason": "PRD approved; delivery pipeline started",
                "superseded_by": child_id,
            },
        )

        self.assertTrue(conn.committed)
        self.assertEqual(result["status"], "superseded")
        update_sql, params = cursor.calls[0]
        self.assertIn("superseded_by_workflow_id", update_sql)
        self.assertEqual(params[0], "superseded")
        self.assertEqual(str(params[2]), child_id)

    def test_complete_workflow_marks_succeeded_without_superseded_by(self):
        gateway = load_gateway()
        workflow_id = "00000000-0000-4000-8000-000000000003"
        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        result = gateway.complete_workflow_record(
            conn,
            workflow_id,
            {"actor": "orchestrator", "reason": "done"},
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(cursor.calls[0][1][0], "succeeded")
        self.assertIsNone(cursor.calls[0][1][2])

    def test_resume_external_event_uses_task_output_when_provided(self):
        gateway = load_gateway()
        workflow_id = "00000000-0000-4000-8000-000000000004"
        task_id = "00000000-0000-4000-8000-000000000005"
        task_uuid = uuid.UUID(task_id)
        cursor = FakeCursor(
            rows=[
                {
                    "id": task_uuid,
                    "expected_event_type": "channel.intake_response",
                }
            ]
        )
        conn = FakeConnection(cursor)

        result = gateway.resume_external_event(
            conn,
            {
                "workflow_id": workflow_id,
                "event_type": "channel.intake_response",
                "payload": {"user_text": "looks good"},
                "task_output": {
                    "clarified_requirements": {"answers": "looks good"},
                },
                "actor": "orchestrator",
            },
        )

        self.assertEqual(result, {"status": "resumed", "task_id": task_id})
        update_sql, params = cursor.calls[1]
        self.assertIn("status = 'succeeded'", update_sql)
        self.assertIn("clarified_requirements", params[0])

    def test_approve_plan_patches_barrier_dependencies(self):
        gateway = load_gateway()
        plan_id = "00000000-0000-4000-8000-000000000010"
        workflow_id = "00000000-0000-4000-8000-000000000011"
        plan_row = {
            "id": uuid.UUID(plan_id),
            "workflow_id": uuid.UUID(workflow_id),
            "approval_status": "pending",
            "proposed_tasks": [
                {"step_name": "implement_TFS-1", "agent_id": "dev-agent", "input": {}},
                {"step_name": "implement_TFS-2", "agent_id": "dev-agent", "input": {}},
            ],
        }

        class ScriptedCursor:
            def __init__(self):
                self.calls = []
                self._last_sql = ""

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                self._last_sql = sql

            def fetchone(self):
                if "FROM workflow_plans" in self._last_sql:
                    return plan_row
                return None

            def fetchall(self):
                if "RETURNING id" in self._last_sql and "depends_on" in self._last_sql:
                    return [{"id": "barrier-1"}]
                return []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        cursor = ScriptedCursor()
        conn = FakeConnection(cursor)

        result = gateway.approve_workflow_plan(
            conn, {"plan_id": plan_id, "actor": "orchestrator"}
        )

        self.assertEqual(len(result["task_ids"]), 2)
        self.assertEqual(result["barrier_patched"], ["barrier-1"])
        # The barrier UPDATE must target collect_delivery_results with created ids.
        barrier_updates = [
            (sql, params)
            for sql, params in cursor.calls
            if "depends_on" in sql
            and "RETURNING id" in sql
            and params
            and params[-1] == "collect_delivery_results"
        ]
        self.assertEqual(len(barrier_updates), 1)
        _, params = barrier_updates[0]
        self.assertEqual(params[0], result["task_ids"])
        self.assertEqual(str(params[1]), workflow_id)


class QueueCursor(FakeCursor):
    def __init__(self, fetches):
        super().__init__()
        self.fetches = list(fetches)

    def fetchone(self):
        return self.fetches.pop(0)

    def fetchall(self):
        return self.fetches.pop(0)


class WorkflowCompletionTests(unittest.TestCase):
    def _complete(self, workflow_status, task_statuses):
        gateway = load_gateway()
        cursor = QueueCursor(
            [
                {"status": workflow_status},
                [{"status": status} for status in task_statuses],
                None,
            ]
        )
        result = gateway.maybe_complete_workflow(cursor, "wf-1", "orchestrator")
        updates = [sql for sql, _params in cursor.calls if "UPDATE workflows" in sql]
        return result, updates

    def test_succeeded_and_cancelled_tasks_complete_the_workflow(self):
        result, updates = self._complete("running", ["succeeded", "cancelled"])
        self.assertEqual(result, "succeeded")
        self.assertEqual(len(updates), 1)
        self.assertIn("completed_at", updates[0])

    def test_superseded_revision_without_a_later_task_stays_running(self):
        gateway = load_gateway()
        cursor = QueueCursor(
            [
                {"status": "running"},
                [{"status": "succeeded"}, {"status": "cancelled"}],
                {"waiting": 1},
            ]
        )
        result = gateway.maybe_complete_workflow(cursor, "wf-1", "orchestrator")
        updates = [sql for sql, _params in cursor.calls if "UPDATE workflows" in sql]
        self.assertIsNone(result)
        self.assertEqual(updates, [])

    def test_failed_task_marks_the_workflow_failed(self):
        result, _updates = self._complete("running", ["succeeded", "failed"])
        self.assertEqual(result, "failed")

    def test_blocked_task_leaves_the_workflow_running(self):
        result, updates = self._complete("running", ["succeeded", "blocked"])
        self.assertIsNone(result)
        self.assertEqual(updates, [])

    def test_cancelled_workflow_stays_cancelled(self):
        result, updates = self._complete("cancelled", ["cancelled"])
        self.assertIsNone(result)
        self.assertEqual(updates, [])

    def test_only_cancelled_tasks_leave_a_running_workflow(self):
        result, updates = self._complete("running", ["cancelled", "skipped"])
        self.assertIsNone(result)
        self.assertEqual(updates, [])

    def test_new_task_reopens_a_completed_workflow(self):
        gateway = load_gateway()
        cursor = QueueCursor([{"id": "wf-1"}])
        gateway._reopen_workflow_for_new_tasks(cursor, "wf-1")
        updates = [sql for sql, _params in cursor.calls if "UPDATE workflows" in sql]
        self.assertEqual(len(updates), 1)
        self.assertIn("running", updates[0])

    def test_new_task_leaves_a_running_workflow_alone(self):
        gateway = load_gateway()
        cursor = QueueCursor([None])
        gateway._reopen_workflow_for_new_tasks(cursor, "wf-1")
        self.assertFalse(any("session_audit_log" in sql for sql, _params in cursor.calls))

    def test_sweep_completes_a_settled_running_workflow(self):
        gateway = load_gateway()
        cursor = QueueCursor(
            [
                [{"id": "wf-1"}],
                {"status": "running"},
                [{"status": "succeeded"}],
            ]
        )
        changed = gateway.sweep_settled_workflows(FakeConnection(cursor))
        self.assertEqual(changed, 1)


class PoolAndServerTests(unittest.TestCase):
    def test_connect_db_never_exceeds_pool_max(self):
        import sys
        import threading
        import types

        gateway = load_gateway()
        gateway._pool = None

        class FakeCheckout:
            def __init__(self, pool):
                self.pool = pool

            def __enter__(self):
                self.pool._sem.acquire()
                with self.pool._lock:
                    self.pool.checked_out += 1
                    self.pool.peak = max(self.pool.peak, self.pool.checked_out)
                return self

            def __exit__(self, exc_type, exc, tb):
                with self.pool._lock:
                    self.pool.checked_out -= 1
                self.pool._sem.release()
                return False

        class FakePool:
            check_connection = staticmethod(lambda conn: True)

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.max_size = kwargs["max_size"]
                self.checked_out = 0
                self.peak = 0
                self._lock = threading.Lock()
                self._sem = threading.Semaphore(self.max_size)

            def connection(self):
                return FakeCheckout(self)

            def close(self):
                return None

        module = types.ModuleType("psycopg_pool")
        module.ConnectionPool = FakePool
        rows = types.ModuleType("psycopg.rows")
        rows.dict_row = object()
        psycopg_mod = types.ModuleType("psycopg")
        psycopg_mod.rows = rows
        previous = sys.modules.get("psycopg_pool")
        previous_psycopg = sys.modules.get("psycopg")
        previous_rows = sys.modules.get("psycopg.rows")
        sys.modules["psycopg_pool"] = module
        sys.modules["psycopg"] = psycopg_mod
        sys.modules["psycopg.rows"] = rows
        os_max = os.environ.get("MEMORY_API_DB_POOL_MAX")
        os.environ["MEMORY_API_DB_POOL_MAX"] = "2"
        try:
            started = threading.Barrier(8)
            errors: list[BaseException] = []

            def worker():
                started.wait(timeout=2)
                try:
                    with gateway.connect_db():
                        threading.Event().wait(0.05)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(errors)
            pool = gateway.get_pool()
            self.assertLessEqual(pool.peak, 2)
            self.assertEqual(pool.kwargs["max_lifetime"], 1800)
            self.assertIs(pool.kwargs["check"], FakePool.check_connection)
        finally:
            gateway._pool = None
            if os_max is None:
                os.environ.pop("MEMORY_API_DB_POOL_MAX", None)
            else:
                os.environ["MEMORY_API_DB_POOL_MAX"] = os_max
            if previous is None:
                sys.modules.pop("psycopg_pool", None)
            else:
                sys.modules["psycopg_pool"] = previous
            if previous_psycopg is None:
                sys.modules.pop("psycopg", None)
            else:
                sys.modules["psycopg"] = previous_psycopg
            if previous_rows is None:
                sys.modules.pop("psycopg.rows", None)
            else:
                sys.modules["psycopg.rows"] = previous_rows

    def test_main_serves_on_a_thread_pool(self):
        gateway = load_gateway()
        seen: dict = {}

        class Server:
            def __init__(self, address, handler):
                seen["address"] = address
                seen["server"] = self
                self.daemon_threads = False

            def serve_forever(self):
                return None

        original = getattr(gateway, "ThreadingHTTPServer", None)
        gateway.ThreadingHTTPServer = Server
        try:
            gateway.main()
        finally:
            if original is None:
                delattr(gateway, "ThreadingHTTPServer")
            else:
                gateway.ThreadingHTTPServer = original
        self.assertEqual(seen["address"][0], "127.0.0.1")
        self.assertTrue(seen["server"].daemon_threads)


if __name__ == "__main__":
    unittest.main()
