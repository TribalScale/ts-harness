"""Tests for delivery program memory-gateway functions."""

from __future__ import annotations

import importlib.util
import json
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
    def __init__(self, rows=None, fetchone_rows=None):
        self.rows = rows or []
        self.fetchone_rows = list(fetchone_rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        if self.fetchone_rows:
            return self.fetchone_rows.pop(0)
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


class DeliveryProgramApiTests(unittest.TestCase):
    def test_create_delivery_program_requires_summary(self):
        gateway = load_gateway()
        conn = FakeConnection(FakeCursor())
        with self.assertRaises(ValueError):
            gateway.create_delivery_program(conn, {"entry_phase": "prd", "actor": "test"})

    def test_create_delivery_program_validates_phase(self):
        gateway = load_gateway()
        conn = FakeConnection(FakeCursor())
        with self.assertRaises(ValueError):
            gateway.create_delivery_program(
                conn,
                {"summary": "test", "entry_phase": "invalid"},
            )

    def test_program_metadata_artifacts(self):
        gateway = load_gateway()
        artifacts = gateway._program_metadata_artifacts({"artifacts": {"approved_prd": {"x": 1}}})
        self.assertEqual(artifacts["approved_prd"]["x"], 1)

    def test_phase_state_from_workflow_succeeded(self):
        gateway = load_gateway()
        wf_id = str(uuid.uuid4())
        state = gateway._phase_state_from_workflow(
            {"id": wf_id, "status": "succeeded", "workflow_type": "prd_request"},
            [],
        )
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["workflow_id"], wf_id)

    def test_list_delivery_programs_filters_and_paginates(self):
        gateway = load_gateway()
        pid = uuid.uuid4()
        rows = [
            {
                "id": pid,
                "tenant_id": "default",
                "summary": "Ship feature",
                "display_name": "Ship",
                "binding_key": "web:1",
                "source_channel": "web",
                "requester_actor_id": "user-1",
                "entry_phase": "prd",
                "current_phase": "prd",
                "status": "running",
                "metadata": {"artifacts": {"approved_prd": True}},
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-02T00:00:00Z",
            }
        ]
        cursor = FakeCursor(rows=rows, fetchone_rows=[{"count": 1}])
        conn = FakeConnection(cursor)
        result = gateway.list_delivery_programs(
            conn, tenant_id="default", status="running", limit=10, offset=0
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["limit"], 10)
        self.assertEqual(len(result["programs"]), 1)
        self.assertEqual(result["programs"][0]["id"], str(pid))
        self.assertEqual(result["programs"][0]["artifacts"]["approved_prd"], True)
        # COUNT then SELECT
        self.assertEqual(len(cursor.calls), 2)
        self.assertIn("tenant_id = %s", cursor.calls[0][0])
        self.assertIn("status = %s", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1], ("default", "running"))
        self.assertEqual(cursor.calls[1][1], ("default", "running", 10, 0))

    def test_list_delivery_programs_rejects_bad_status(self):
        gateway = load_gateway()
        conn = FakeConnection(FakeCursor())
        with self.assertRaises(ValueError):
            gateway.list_delivery_programs(conn, status="nope")


MINI_SNAPSHOT = {
    "program_type": "custom_delivery",
    "definition_version": "2.0.0",
    "display_name": "Custom delivery",
    "startable_nodes": ["intake", "build"],
    "nodes": [
        {
            "id": "intake",
            "default_workflow_type": "prd_request",
            "workflow_variants": [],
            "required_artifacts": [],
            "produces_artifacts": ["approved_prd"],
            "input_bindings": [],
            "completion_adapter": "extract_prd_outputs",
            "terminal": False,
        },
        {
            "id": "plan_a",
            "default_workflow_type": "jira_from_prd",
            "workflow_variants": [],
            "required_artifacts": ["approved_prd"],
            "produces_artifacts": [],
            "input_bindings": [],
            "completion_adapter": "extract_jira_outputs",
            "terminal": False,
        },
        {
            "id": "plan_b",
            "default_workflow_type": "work_orders_from_prd",
            "workflow_variants": [],
            "required_artifacts": ["approved_prd"],
            "produces_artifacts": [],
            "input_bindings": [],
            "completion_adapter": "extract_work_orders_outputs",
            "terminal": False,
        },
        {
            "id": "build",
            "default_workflow_type": "implement_delivery",
            "workflow_variants": [],
            "required_artifacts": [],
            "produces_artifacts": [],
            "input_bindings": [],
            "completion_adapter": "extract_implement_outputs",
            "terminal": True,
        },
    ],
    "edges": [
        {
            "source": "intake",
            "target": "plan_a",
            "mode": "manual",
            "confirm_required": True,
            "signals": [],
            "artifact_patch": {},
        },
        {
            "source": "intake",
            "target": "plan_b",
            "mode": "manual",
            "confirm_required": True,
            "signals": [],
            "artifact_patch": {},
        },
        {
            "source": "plan_a",
            "target": "build",
            "mode": "manual",
            "confirm_required": False,
            "signals": [],
            "artifact_patch": {},
        },
        {
            "source": "plan_b",
            "target": "build",
            "mode": "automatic",
            "confirm_required": False,
            "signals": [],
            "artifact_patch": {},
        },
    ],
}

RESOLVED_VERSIONS = {
    "intake": {"workflow_type": "prd_request", "definition_version": "1.0.0"},
    "plan_a": {"workflow_type": "jira_from_prd", "definition_version": "1.0.0"},
    "plan_b": {"workflow_type": "work_orders_from_prd", "definition_version": "1.1.0"},
    "build": {"workflow_type": "implement_delivery", "definition_version": "2.0.0"},
}


class ProgramDefinitionPinningApiTests(unittest.TestCase):
    def test_create_persists_definition_pins_atomically(self):
        gateway = load_gateway()
        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        result = gateway.create_delivery_program(
            conn,
            {
                "summary": "Pinned run",
                "entry_phase": "intake",
                "program_type": "custom_delivery",
                "program_definition_version": "2.0.0",
                "program_definition_revision": "rev-abc",
                "program_definition_snapshot": MINI_SNAPSHOT,
                "resolved_workflow_versions": RESOLVED_VERSIONS,
                "actor": "test",
            },
        )
        self.assertTrue(conn.committed)
        self.assertEqual(result["program_type"], "custom_delivery")
        self.assertEqual(result["program_definition_version"], "2.0.0")
        self.assertEqual(result["program_definition_revision"], "rev-abc")
        insert_sql, params = cursor.calls[0]
        self.assertIn("program_definition_snapshot", insert_sql)
        self.assertIn("resolved_workflow_versions", insert_sql)
        self.assertIn("program_type", insert_sql)
        self.assertIn("custom_delivery", params)
        self.assertIn("2.0.0", params)
        self.assertIn("rev-abc", params)
        self.assertTrue(any(isinstance(p, str) and "intake" in p for p in params))

    def test_create_validates_entry_against_snapshot_nodes(self):
        gateway = load_gateway()
        conn = FakeConnection(FakeCursor())
        with self.assertRaises(ValueError) as ctx:
            gateway.create_delivery_program(
                conn,
                {
                    "summary": "bad entry",
                    "entry_phase": "prd",
                    "program_definition_snapshot": MINI_SNAPSHOT,
                },
            )
        self.assertIn("entry_phase", str(ctx.exception).lower())

    def test_create_without_snapshot_still_uses_legacy_phases(self):
        gateway = load_gateway()
        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        result = gateway.create_delivery_program(
            conn,
            {"summary": "legacy", "entry_phase": "prd", "actor": "test"},
        )
        self.assertEqual(result["entry_phase"], "prd")
        self.assertIsNone(result.get("program_type"))
        # Pins columns may be present but values should be null for legacy creates.
        params = cursor.calls[0][1]
        self.assertIn(None, params)

    def test_legacy_compat_topology_matches_implement_terminal(self):
        gateway = load_gateway()
        snap = gateway._legacy_compatibility_topology()
        self.assertEqual(snap["definition_version"], "legacy-compat")
        node_ids = [n["id"] for n in snap["nodes"]]
        self.assertEqual(node_ids, ["prd", "jira", "work_orders", "implement"])
        self.assertEqual(snap["nodes"][0]["default_workflow_type"], "prd_request")
        edges = {(e["source"], e["target"]) for e in snap["edges"]}
        self.assertEqual(
            edges,
            {
                ("prd", "jira"),
                ("prd", "work_orders"),
                ("jira", "implement"),
                ("work_orders", "implement"),
            },
        )

    def test_advance_validates_target_against_snapshot(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "current_phase": "intake",
            "program_definition_snapshot": MINI_SNAPSHOT,
        }
        cursor = FakeCursor(fetchone_rows=[program])
        conn = FakeConnection(cursor)
        with self.assertRaises(ValueError):
            gateway.advance_delivery_program(
                conn,
                program_id,
                {"target_phase": "deploy", "actor": "test"},
            )

    def test_advance_allows_snapshot_edge_target(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "current_phase": "intake",
            "program_definition_snapshot": MINI_SNAPSHOT,
        }
        cursor = FakeCursor(fetchone_rows=[program])
        conn = FakeConnection(cursor)
        result = gateway.advance_delivery_program(
            conn,
            program_id,
            {"target_phase": "plan_b", "actor": "test"},
        )
        self.assertEqual(result["current_phase"], "plan_b")
        self.assertTrue(conn.committed)

    def test_advance_allows_same_phase_reentry(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        snapshot = {
            "nodes": [
                {"id": "implement", "default_workflow_type": "implement_work_orders"},
                {"id": "deploy", "default_workflow_type": "deploy_release"},
            ],
            "edges": [{"source": "implement", "target": "deploy"}],
        }
        program = {
            "id": program_id,
            "current_phase": "implement",
            "program_definition_snapshot": snapshot,
        }
        cursor = FakeCursor(fetchone_rows=[program])
        conn = FakeConnection(cursor)
        result = gateway.advance_delivery_program(
            conn,
            program_id,
            {"target_phase": "implement", "actor": "test"},
        )
        self.assertEqual(result["current_phase"], "implement")
        self.assertTrue(conn.committed)

    def test_link_workflow_validates_phase_against_snapshot(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "program_definition_snapshot": MINI_SNAPSHOT,
        }
        cursor = FakeCursor(fetchone_rows=[program])
        conn = FakeConnection(cursor)
        with self.assertRaises(ValueError):
            gateway.link_workflow_to_program(
                conn,
                program_id,
                {
                    "workflow_id": workflow_id,
                    "program_phase": "prd",
                    "actor": "test",
                },
            )

    def test_status_exposes_definition_identity_and_snapshot_phase_order(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "summary": "Pinned",
            "entry_phase": "intake",
            "current_phase": "intake",
            "status": "running",
            "metadata": {},
            "program_type": "custom_delivery",
            "program_definition_version": "2.0.0",
            "program_definition_revision": "rev-abc",
            "program_definition_snapshot": MINI_SNAPSHOT,
            "resolved_workflow_versions": RESOLVED_VERSIONS,
        }

        class StatusCursor(FakeCursor):
            def __init__(self):
                super().__init__()
                self._program_fetched = False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                upper = sql.upper()
                if "FROM DELIVERY_PROGRAMS" in upper and "SELECT *" in upper:
                    self.rows = [program]
                elif "FROM WORKFLOWS" in upper:
                    self.rows = []
                elif "FROM WORKFLOW_TASKS" in upper:
                    self.rows = []

            def fetchone(self):
                return program

            def fetchall(self):
                return list(self.rows)

        conn = FakeConnection(StatusCursor())
        status = gateway.get_delivery_program_status(conn, program_id)
        self.assertEqual(status["program_type"], "custom_delivery")
        self.assertEqual(status["program_definition_version"], "2.0.0")
        self.assertEqual(status["program_definition_revision"], "rev-abc")
        self.assertEqual(
            status["phase_order"],
            ["intake", "plan_a", "plan_b", "build"],
        )
        self.assertIn("intake", status["phases"])
        self.assertIn("plan_a", status["phases"])
        self.assertNotIn("prd", status["phases"])
        self.assertEqual(status["topology"]["program_type"], "custom_delivery")
        self.assertEqual(len(status["topology"]["nodes"]), 4)
        self.assertEqual(status["resolved_workflow_versions"]["intake"]["workflow_type"], "prd_request")
        self.assertEqual(status["phases"]["intake"].get("workflow_definition_version"), "1.0.0")

    def test_status_branch_visibility_hides_untaken_sibling(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        wf_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "summary": "Branch",
            "entry_phase": "intake",
            "current_phase": "plan_a",
            "status": "running",
            "metadata": {},
            "program_type": "custom_delivery",
            "program_definition_version": "2.0.0",
            "program_definition_revision": "rev-abc",
            "program_definition_snapshot": MINI_SNAPSHOT,
        }
        workflows = [
            {
                "id": wf_id,
                "program_phase": "plan_a",
                "status": "running",
                "workflow_type": "jira_from_prd",
            }
        ]

        class StatusCursor(FakeCursor):
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                upper = sql.upper()
                if "FROM DELIVERY_PROGRAMS" in upper:
                    self.rows = [program]
                elif "FROM WORKFLOWS" in upper:
                    self.rows = list(workflows)
                elif "FROM WORKFLOW_TASKS" in upper:
                    self.rows = []

            def fetchone(self):
                return program

            def fetchall(self):
                return list(self.rows)

        conn = FakeConnection(StatusCursor())
        status = gateway.get_delivery_program_status(conn, program_id)
        self.assertEqual(status["phase_order"], ["intake", "plan_a", "build"])
        self.assertNotIn("plan_b", status["phase_order"])
        self.assertIn("plan_a", status["phases"])
        # Full topology still available for expanders, but phase_order is the visible branch.
        self.assertIn("plan_b", status["phases"])

    def test_status_legacy_null_snapshot_uses_compat_topology(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "summary": "Legacy",
            "entry_phase": "prd",
            "current_phase": "prd",
            "status": "running",
            "metadata": {},
            "program_type": None,
            "program_definition_version": None,
            "program_definition_revision": None,
            "program_definition_snapshot": None,
        }

        class StatusCursor(FakeCursor):
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                upper = sql.upper()
                if "FROM DELIVERY_PROGRAMS" in upper:
                    self.rows = [program]
                elif "FROM WORKFLOWS" in upper:
                    self.rows = []
                elif "FROM WORKFLOW_TASKS" in upper:
                    self.rows = []

            def fetchone(self):
                return program

            def fetchall(self):
                return list(self.rows)

        conn = FakeConnection(StatusCursor())
        status = gateway.get_delivery_program_status(conn, program_id)
        self.assertEqual(
            status["phase_order"],
            ["prd", "jira", "work_orders", "implement"],
        )
        for phase in ("prd", "jira", "work_orders", "implement"):
            self.assertIn(phase, status["phases"])


    def test_cancelled_status_survives_a_succeeded_terminal_phase(self):
        gateway = load_gateway()

        def status_for(stored: str) -> str:
            program_id = str(uuid.uuid4())
            program = {
                "id": program_id,
                "summary": "Cancelled delivery",
                "entry_phase": "intake",
                "current_phase": "build",
                "status": stored,
                "metadata": {},
                "program_type": "custom_delivery",
                "program_definition_version": "2.0.0",
                "program_definition_revision": "rev-abc",
                "program_definition_snapshot": MINI_SNAPSHOT,
            }
            workflow = {
                "id": str(uuid.uuid4()),
                "program_phase": "build",
                "status": "succeeded",
                "workflow_type": "implement_delivery",
            }

            class StatusCursor(FakeCursor):
                def execute(self, sql, params=None):
                    self.calls.append((sql, params))
                    upper = sql.upper()
                    if "FROM DELIVERY_PROGRAMS" in upper and "SELECT *" in upper:
                        self.rows = [program]
                    elif "FROM WORKFLOWS" in upper:
                        self.rows = [workflow]
                    else:
                        self.rows = []

                def fetchone(self):
                    return program

                def fetchall(self):
                    return list(self.rows)

            status = gateway.get_delivery_program_status(
                FakeConnection(StatusCursor()),
                program_id,
            )
            return status["status"]

        self.assertEqual(status_for("cancelled"), "cancelled")
        self.assertEqual(status_for("running"), "succeeded")

    def test_rollback_stays_off_the_phase_strip_until_it_starts(self):
        gateway = load_gateway()
        snapshot = json.loads(
            (REPO_ROOT / "config" / "programs" / "software_delivery.json").read_text()
        )

        def phase_order(extra_workflows: list[dict]) -> list[str]:
            program_id = str(uuid.uuid4())
            program = {
                "id": program_id,
                "summary": "Delivery",
                "entry_phase": "prd",
                "current_phase": "prd",
                "status": "running",
                "metadata": {},
                "program_type": "software_delivery",
                "program_definition_version": snapshot["definition_version"],
                "program_definition_snapshot": snapshot,
            }
            workflows = [
                {
                    "id": str(uuid.uuid4()),
                    "program_phase": "prd",
                    "status": "running",
                    "workflow_type": "prd_request",
                },
                *extra_workflows,
            ]

            class StatusCursor(FakeCursor):
                def execute(self, sql, params=None):
                    self.calls.append((sql, params))
                    upper = sql.upper()
                    if "FROM DELIVERY_PROGRAMS" in upper and "SELECT *" in upper:
                        self.rows = [program]
                    elif "FROM WORKFLOWS" in upper:
                        self.rows = workflows
                    else:
                        self.rows = []

                def fetchone(self):
                    return program

                def fetchall(self):
                    return list(self.rows)

            status = gateway.get_delivery_program_status(
                FakeConnection(StatusCursor()),
                program_id,
            )
            return status["phase_order"]

        self.assertEqual(
            phase_order([]),
            ["prd", "jira", "work_orders", "implement", "release"],
        )
        self.assertEqual(
            phase_order(
                [
                    {
                        "id": str(uuid.uuid4()),
                        "program_phase": "rollback",
                        "status": "running",
                        "workflow_type": "rollback_release",
                    }
                ]
            ),
            ["prd", "jira", "work_orders", "implement", "release", "rollback"],
        )


class MemoryClientProgramPinningTests(unittest.TestCase):
    def test_get_program_quotes_spaces_in_the_path(self):
        from ts_harness.memory.client import MemoryClient

        captured: dict = {}

        class StubClient(MemoryClient):
            def _request(self, method, path, body=None):  # type: ignore[override]
                captured["path"] = path
                return {"program": {"id": "prog-1"}}

        StubClient().get_program("My Program")
        self.assertEqual(captured["path"], "/programs/My%20Program")

    def test_create_program_forwards_definition_pins(self):
        from ts_harness.memory.client import MemoryClient

        captured: dict = {}

        class StubClient(MemoryClient):
            def _request(self, method, path, body=None):  # type: ignore[override]
                captured["method"] = method
                captured["path"] = path
                captured["body"] = body
                return {"id": "p1"}

        client = StubClient(base_url="http://example")
        client.create_program(
            summary="Pinned",
            entry_phase="intake",
            program_type="custom_delivery",
            program_definition_version="2.0.0",
            program_definition_revision="rev-abc",
            program_definition_snapshot=MINI_SNAPSHOT,
            resolved_workflow_versions=RESOLVED_VERSIONS,
        )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/programs/create")
        body = captured["body"]
        self.assertEqual(body["program_type"], "custom_delivery")
        self.assertEqual(body["program_definition_version"], "2.0.0")
        self.assertEqual(body["program_definition_revision"], "rev-abc")
        self.assertEqual(body["program_definition_snapshot"]["program_type"], "custom_delivery")
        self.assertEqual(body["resolved_workflow_versions"]["intake"]["workflow_type"], "prd_request")


class ProgramProjectMemoryApiTests(unittest.TestCase):
    def test_create_delivery_program_persists_project_id(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        cursor = FakeCursor()
        conn = FakeConnection(cursor)
        result = gateway.create_delivery_program(
            conn,
            {
                "id": program_id,
                "summary": "Project A",
                "entry_phase": "prd",
                "project_id": "TFS",
                "project_id_source": "user",
                "tenant_id": "default",
                "actor": "test",
            },
        )
        insert_sql, params = next(
            (sql, params) for sql, params in cursor.calls if "INSERT INTO delivery_programs" in sql
        )
        self.assertIn("project_id", insert_sql)
        self.assertIn("TFS", params)
        self.assertEqual(result["project_id"], "TFS")
        self.assertEqual(result["project_id_source"], "user")

    def test_cross_tenant_link_rejected(self):
        gateway = load_gateway()
        from_id = str(uuid.uuid4())
        to_id = str(uuid.uuid4())
        cursor = FakeCursor(
            fetchone_rows=[
                {"id": from_id, "tenant_id": "tenant-a"},
                {"id": to_id, "tenant_id": "tenant-b"},
            ]
        )
        conn = FakeConnection(cursor)
        with self.assertRaises(ValueError) as ctx:
            gateway.create_program_link(
                conn,
                from_id,
                {"to_program_id": to_id, "tenant_id": "tenant-a", "actor": "test"},
            )
        self.assertIn("cross-tenant", str(ctx.exception))

    def test_unlink_deletes_only_that_edge(self):
        gateway = load_gateway()
        from_id = str(uuid.uuid4())
        to_id = str(uuid.uuid4())
        cursor = FakeCursor(fetchone_rows=[{"id": from_id, "tenant_id": "default"}])
        conn = FakeConnection(cursor)
        result = gateway.delete_program_link(conn, from_id, to_id, {"actor": "test"})
        self.assertTrue(result["deleted"])
        delete_sql, delete_params = next(
            (sql, params) for sql, params in cursor.calls if "DELETE FROM program_links" in sql
        )
        self.assertIn("from_program_id = %s AND to_program_id = %s AND tenant_id = %s", delete_sql)
        self.assertEqual(delete_params, (from_id, to_id, "default"))

    def test_exclude_flag_persisted_on_patch(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "tenant_id": "default",
            "project_id": "TFS",
            "project_id_source": "user",
            "excluded_from_autoshare": False,
        }

        class ScriptedCursor(FakeCursor):
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                self._last = sql

            def fetchone(self):
                return program

            def fetchall(self):
                return []

        cursor = ScriptedCursor()
        conn = FakeConnection(cursor)
        gateway.patch_delivery_program(
            conn,
            program_id,
            {"excluded_from_autoshare": True, "tenant_id": "default", "actor": "test"},
        )
        update = next(sql for sql, _ in cursor.calls if "UPDATE delivery_programs" in sql)
        self.assertIn("excluded_from_autoshare = %s", update)
        params = next(params for sql, params in cursor.calls if "UPDATE delivery_programs" in sql)
        self.assertTrue(params[0])

    def test_brief_upsert_replaces_same_key(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        cursor = FakeCursor(
            fetchone_rows=[
                {"id": program_id, "tenant_id": "default", "project_id": "TFS"},
                {"id": str(uuid.uuid4())},
            ]
        )
        conn = FakeConnection(cursor)
        result = gateway.upsert_program_briefs(
            conn,
            program_id,
            {
                "tenant_id": "default",
                "entries": [
                    {
                        "phase": "prd",
                        "kind": "pointer",
                        "topic": "approved_prd",
                        "content": "Approved PRD",
                        "artifact_ref": "approved_prd",
                    }
                ],
            },
        )
        self.assertEqual(len(result["entries"]), 1)
        insert_sql = next(sql for sql, _ in cursor.calls if "INSERT INTO program_briefs" in sql)
        self.assertIn("ON CONFLICT (source_program_id, phase, kind, topic)", insert_sql)


if __name__ == "__main__":
    unittest.main()
