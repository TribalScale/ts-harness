"""Parent/child delivery programs, external refs, roll-up and cascade cancel."""

from __future__ import annotations

import importlib.util
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "scripts" / "memory-gateway.py"


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_hierarchy", GATEWAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoutedCursor:
    """Fake cursor that answers by the first matching SQL fragment."""

    def __init__(self, routes: list[tuple[str, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, Any]] = []
        self._last: Any = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = None
        for fragment, result in self.routes:
            if fragment in sql:
                self._last = result(params) if callable(result) else result
                break

    def fetchone(self):
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return self._last

    def fetchall(self):
        if isinstance(self._last, list):
            return self._last
        return [self._last] if self._last else []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def sql_matching(self, fragment: str) -> list[tuple[str, Any]]:
        return [call for call in self.calls if fragment in call[0]]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def _program_row(program_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": program_id,
        "tenant_id": "default",
        "status": "running",
        "parent_program_id": None,
        "root_program_id": None,
        "current_phase": "prd",
        "metadata": {},
    }
    row.update(overrides)
    return row


class CreateAsChildTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()
        self.parent_id = str(uuid.uuid4())

    def _create(self, parent_row: dict[str, Any]):
        cursor = RoutedCursor([("FROM delivery_programs WHERE id", parent_row)])
        conn = FakeConnection(cursor)
        result = self.gateway.create_delivery_program(
            conn,
            {"summary": "child", "entry_phase": "prd", "parent_program_id": self.parent_id},
        )
        return result, cursor

    def test_child_records_parent_and_root(self):
        result, cursor = self._create(_program_row(self.parent_id))
        self.assertEqual(result["parent_program_id"], self.parent_id)
        self.assertEqual(result["root_program_id"], self.parent_id)
        insert = cursor.sql_matching("INSERT INTO delivery_programs")[0]
        self.assertIn("parent_program_id, root_program_id", insert[0])
        self.assertEqual(insert[1][-2:], (self.parent_id, self.parent_id))
        audits = [call[1][2] for call in cursor.sql_matching("INSERT INTO session_audit_log")]
        self.assertIn("program_child_attached", audits)

    def test_root_is_inherited_from_parent(self):
        root_id = str(uuid.uuid4())
        result, _ = self._create(_program_row(self.parent_id, root_program_id=root_id))
        self.assertEqual(result["root_program_id"], root_id)

    def test_cross_tenant_parent_rejected(self):
        with self.assertRaisesRegex(ValueError, "cross-tenant"):
            self._create(_program_row(self.parent_id, tenant_id="other"))

    def test_cancelled_parent_rejected(self):
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self._create(_program_row(self.parent_id, status="cancelled"))

    def test_no_parent_keeps_columns_null(self):
        cursor = RoutedCursor([])
        conn = FakeConnection(cursor)
        result = self.gateway.create_delivery_program(conn, {"summary": "root", "entry_phase": "prd"})
        self.assertIsNone(result["parent_program_id"])
        insert = cursor.sql_matching("INSERT INTO delivery_programs")[0]
        self.assertEqual(insert[1][-2:], (None, None))


class AdoptParentPatchTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()

    def test_patch_sets_parent_and_rewrites_descendant_roots(self):
        child_id = str(uuid.uuid4())
        parent_id = str(uuid.uuid4())
        rows = {
            child_id: _program_row(child_id),
            parent_id: _program_row(parent_id),
        }
        cursor = RoutedCursor(
            [
                ("SELECT * FROM delivery_programs WHERE id", lambda params: rows.get(params[0])),
                ("FROM workflows", []),
            ]
        )
        conn = FakeConnection(cursor)
        self.gateway.patch_delivery_program(conn, child_id, {"parent_program_id": parent_id})
        update = cursor.sql_matching("UPDATE delivery_programs\n            SET")[0]
        self.assertIn("parent_program_id = %s", update[0])
        self.assertEqual(update[1][:2], (parent_id, parent_id))
        reroot = cursor.sql_matching("SET root_program_id = %s WHERE root_program_id")[0]
        self.assertEqual(reroot[1], (parent_id, child_id))

    def test_patch_refuses_to_replace_existing_parent(self):
        child_id = str(uuid.uuid4())
        cursor = RoutedCursor(
            [("FROM delivery_programs WHERE id", _program_row(child_id, parent_program_id=str(uuid.uuid4())))]
        )
        with self.assertRaisesRegex(ValueError, "already has a parent"):
            self.gateway.patch_delivery_program(
                FakeConnection(cursor), child_id, {"parent_program_id": str(uuid.uuid4())}
            )


class CascadeCancelTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()

    def test_cancel_cascades_to_active_children(self):
        root_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())
        cursor = RoutedCursor(
            [
                ("WITH RECURSIVE tree", [{"id": child_id}]),
                ("FROM workflows", []),
            ]
        )
        conn = FakeConnection(cursor)
        result = self.gateway.cancel_delivery_program(conn, root_id, {"reason": "scope cut"})
        self.assertEqual(result["cancelled_children"], [child_id])
        updated = [call[1][0] for call in cursor.sql_matching("SET status = 'cancelled'")]
        self.assertEqual(updated, [root_id, child_id])
        tree_sql = cursor.sql_matching("WITH RECURSIVE tree")[0][0]
        self.assertIn("p.status IN ('running', 'blocked')", tree_sql)

    def test_cascade_false_only_cancels_self(self):
        root_id = str(uuid.uuid4())
        cursor = RoutedCursor([("FROM workflows", [])])
        result = self.gateway.cancel_delivery_program(
            FakeConnection(cursor), root_id, {"cascade": False}
        )
        self.assertEqual(result["cancelled_children"], [])
        self.assertFalse(cursor.sql_matching("WITH RECURSIVE tree"))


class ExternalRefTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()
        self.program_id = str(uuid.uuid4())

    def _conn(self):
        cursor = RoutedCursor([("FROM delivery_programs WHERE id", _program_row(self.program_id))])
        return FakeConnection(cursor), cursor

    def test_record_upserts_ref(self):
        conn, cursor = self._conn()
        result = self.gateway.record_program_external_ref(
            conn,
            self.program_id,
            {"kind": "pull_request", "ref": "https://github.com/acme/app/pull/7"},
        )
        self.assertEqual(result["state"], "open")
        insert = cursor.sql_matching("INSERT INTO program_external_refs")[0]
        self.assertIn("ON CONFLICT (program_id, kind, ref) DO UPDATE", insert[0])
        self.assertTrue(conn.committed)

    def test_invalid_kind_rejected(self):
        conn, _ = self._conn()
        with self.assertRaisesRegex(ValueError, "kind must be one of"):
            self.gateway.record_program_external_ref(conn, self.program_id, {"kind": "tweet", "ref": "x"})

    def test_cross_tenant_rejected(self):
        conn, _ = self._conn()
        with self.assertRaisesRegex(ValueError, "cross-tenant"):
            self.gateway.record_program_external_ref(
                conn, self.program_id, {"kind": "branch", "ref": "a#b", "tenant_id": "other"}
            )


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()

    def test_rollup_counts_and_pr_urls(self):
        children = [
            {"id": "c1", "status": "running", "artifacts": {"pr_urls": ["https://x/pull/1"]}},
            {"id": "c2", "status": "succeeded", "artifacts": {"pr_url": "https://x/pull/2"}},
        ]
        statuses = {
            "c1": {"status": "blocked", "waiting_on": {"kind": "external"}},
            "c2": {"status": "succeeded"},
        }
        with mock.patch.object(
            self.gateway,
            "get_delivery_program_status",
            side_effect=lambda conn, pid, include_children=True: statuses[pid],
        ):
            rollup = self.gateway._child_rollup(None, children)
        self.assertEqual(rollup["active_child_count"], 1)
        self.assertEqual(rollup["children"][0]["status"], "blocked")
        self.assertEqual(rollup["children"][0]["waiting_on"], {"kind": "external"})
        self.assertEqual(rollup["children"][1]["pr_urls"], ["https://x/pull/2"])


class PointerAdoptionTests(unittest.TestCase):
    def test_pick_output_parses_program_pointer(self):
        from ts_harness.workflows.program_adapters import build_default_adapter_registry

        registry = build_default_adapter_registry()
        patch = registry.extract_outputs(
            "extract_pick_ticket_outputs",
            workflow_type="pick_assigned_jira",
            tasks=[
                {
                    "status": "succeeded",
                    "output": {
                        "jira_issue_key": "ACME-7",
                        "jira_issue_description": "AC\n\nWO: WO-1\nRepo: acme/app@dev\nProgram: root-123\n",
                    },
                }
            ],
            artifacts={},
        )
        self.assertEqual(patch["owner_program_id"], "root-123")

    def test_pick_output_without_pointer_has_no_owner(self):
        from ts_harness.workflows.program_adapters import owner_program_from_pick_output

        self.assertIsNone(owner_program_from_pick_output({"jira_issue_description": "just text"}))

    def _service(self, client):
        from ts_harness.workflows.delivery_program import DeliveryProgramService

        return DeliveryProgramService(client=client)

    def test_service_adopts_owner_when_unparented(self):
        client = mock.Mock()
        service = self._service(client)
        adopted = service._adopt_owner_program(
            {"id": "child", "tenant_id": "acme"},
            {"owner_program_id": "root-123"},
            actor="runner",
        )
        self.assertEqual(adopted, "root-123")
        client.patch_program.assert_called_once_with(
            "child", tenant_id="acme", parent_program_id="root-123", actor="runner"
        )

    def test_service_keeps_existing_parent(self):
        client = mock.Mock()
        service = self._service(client)
        adopted = service._adopt_owner_program(
            {"id": "child", "parent_program_id": "p0"},
            {"owner_program_id": "root-123"},
            actor="runner",
        )
        self.assertIsNone(adopted)
        client.patch_program.assert_not_called()

    def test_service_records_jira_and_pr_refs(self):
        client = mock.Mock()
        service = self._service(client)
        recorded = service._record_artifact_refs(
            "prog",
            workflow_id="wf",
            artifact_patch={
                "jira_epic_key": "ACME-1",
                "jira_story_keys": ["ACME-2"],
                "pr_urls": ["https://github.com/acme/app/pull/3"],
                "pr_url": "https://github.com/acme/app/pull/3",
            },
            actor="runner",
        )
        self.assertEqual(
            recorded,
            [
                ("jira_issue", "ACME-1"),
                ("jira_issue", "ACME-2"),
                ("pull_request", "https://github.com/acme/app/pull/3"),
            ],
        )


class StartParentResolutionTests(unittest.TestCase):
    def test_content_program_attaches_to_thread_software_delivery(self):
        from ts_harness.tools.workflow_tools import resolve_start_parent_program

        parent = resolve_start_parent_program(
            "content_to_jira_work_orders",
            explicit=None,
            session_metadata={"program_id": "sd-1", "program_type": "software_delivery"},
        )
        self.assertEqual(parent, "sd-1")

    def test_bug_program_attaches_to_thread_software_delivery(self):
        from ts_harness.tools.workflow_tools import resolve_start_parent_program

        parent = resolve_start_parent_program(
            "bug_to_jira_work_orders",
            explicit=None,
            session_metadata={"program_id": "sd-1", "program_type": "software_delivery"},
        )
        self.assertEqual(parent, "sd-1")

    def test_explicit_parent_wins(self):
        from ts_harness.tools.workflow_tools import resolve_start_parent_program

        parent = resolve_start_parent_program(
            "jira_to_pr",
            explicit="p-9",
            session_metadata={"program_id": "sd-1", "program_type": "software_delivery"},
        )
        self.assertEqual(parent, "p-9")

    def test_unrelated_program_type_has_no_parent(self):
        from ts_harness.tools.workflow_tools import resolve_start_parent_program

        self.assertIsNone(
            resolve_start_parent_program(
                "software_delivery",
                explicit=None,
                session_metadata={"program_id": "sd-1", "program_type": "software_delivery"},
            )
        )


class CodingRefsTests(unittest.TestCase):
    def test_branch_and_pr_recorded(self):
        from ts_harness.tools.coding_backend_tool import record_coding_refs

        client = mock.Mock()
        warnings = record_coding_refs(
            "prog",
            workflow_id="wf",
            repo_url="https://github.com/Acme/App.git",
            branch="feat/wo-1",
            pr_url="https://github.com/acme/app/pull/4",
            work_order_id="WO-1",
            client=client,
        )
        self.assertEqual(warnings, [])
        kinds = [call.kwargs["kind"] for call in client.record_program_external_ref.call_args_list]
        refs = [call.kwargs["ref"] for call in client.record_program_external_ref.call_args_list]
        self.assertEqual(kinds, ["branch", "pull_request"])
        self.assertEqual(refs[0], "acme/app#feat/wo-1")

    def test_ref_failure_is_warning_only(self):
        from ts_harness.tools.coding_backend_tool import record_coding_refs

        client = mock.Mock()
        client.record_program_external_ref.side_effect = RuntimeError("down")
        warnings = record_coding_refs(
            "prog",
            workflow_id=None,
            repo_url="acme/app",
            branch="b",
            pr_url=None,
            work_order_id="WO-1",
            client=client,
        )
        self.assertEqual(warnings, ["external_ref_failed:branch"])


if __name__ == "__main__":
    unittest.main()
