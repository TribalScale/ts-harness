"""Per-program evidence export: gateway query, signed document, and admin route."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from ts_harness.gateway import asgi_app
from ts_harness.gateway.program_evidence import (
    EVIDENCE_SCHEMA,
    build_evidence_document,
    evidence_sha256,
)
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_ID = "11111111-1111-1111-1111-111111111111"
CHILD_ID = "22222222-2222-2222-2222-222222222222"
WF_ROOT = "33333333-3333-3333-3333-333333333333"
WF_CHILD = "44444444-4444-4444-4444-444444444444"
TASK_ID = "55555555-5555-5555-5555-555555555555"


def load_gateway():
    spec = importlib.util.spec_from_file_location(
        "memory_gateway_evidence", REPO_ROOT / "scripts" / "memory-gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self, routes: list[tuple[str, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, Any]] = []
        self._last: Any = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = next((result for fragment, result in self.routes if fragment in sql), None)

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([self._last] if self._last else [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def _evidence_cursor(audit_rows: list[dict[str, Any]] | None = None) -> Cursor:
    return Cursor(
        [
            ("WITH RECURSIVE tree", [{"id": CHILD_ID}]),
            ("FROM delivery_programs WHERE id = ANY", [{"id": CHILD_ID, "parent_program_id": ROOT_ID}]),
            (
                "FROM delivery_programs WHERE id = %s",
                {
                    "id": ROOT_ID,
                    "tenant_id": "default",
                    "metadata": {"artifacts": {"compensation": {"status": "completed", "actions": []}}},
                },
            ),
            ("FROM workflows", [{"id": WF_ROOT}, {"id": WF_CHILD}]),
            ("FROM workflow_tasks", [{"id": TASK_ID}]),
            ("FROM workflow_decisions", [{"id": "d1", "kind": "gate", "decision": "approved"}]),
            (
                "FROM program_external_refs",
                [
                    {"id": "r1", "kind": "pull_request", "ref": "https://github.com/acme/app/pull/1"},
                    {"id": "r2", "kind": "jira_issue", "ref": "APP-1"},
                    {"id": "r3", "kind": "deployment", "ref": "prod-1"},
                ],
            ),
            ("FROM program_releases", []),
            ("FROM session_audit_log", audit_rows if audit_rows is not None else [{"id": "a1"}, {"id": "a2"}]),
        ]
    )


class GatewayEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()

    def test_evidence_covers_the_program_tree(self):
        cursor = _evidence_cursor()

        evidence = self.gateway.get_program_evidence(Conn(cursor), ROOT_ID)

        self.assertEqual([child["id"] for child in evidence["children"]], [CHILD_ID])
        self.assertEqual(len(evidence["workflows"]), 2)
        self.assertEqual([ref["id"] for ref in evidence["pull_requests"]], ["r1"])
        self.assertEqual([ref["id"] for ref in evidence["jira"]], ["r2"])
        self.assertEqual([ref["id"] for ref in evidence["other_refs"]], ["r3"])
        self.assertEqual(evidence["compensation"]["status"], "completed")
        self.assertFalse(evidence["audit_truncated"])
        for fragment in ("FROM workflows", "FROM workflow_decisions", "FROM program_external_refs", "FROM program_releases"):
            params = next(p for sql, p in cursor.calls if fragment in sql)
            self.assertEqual(params[0], [ROOT_ID, CHILD_ID], fragment)

    def test_audit_timeline_matches_program_workflow_and_task_ids_in_order(self):
        cursor = _evidence_cursor()

        self.gateway.get_program_evidence(Conn(cursor), ROOT_ID)

        sql, params = next((s, p) for s, p in cursor.calls if "FROM session_audit_log" in s)
        self.assertIn("details->>'program_id' = ANY", sql)
        self.assertIn("details->>'workflow_id' = ANY", sql)
        self.assertIn("details->>'task_id' = ANY", sql)
        self.assertIn("ORDER BY occurred_at, id", sql)
        self.assertEqual(params[0], [ROOT_ID, CHILD_ID])
        self.assertEqual(params[1], [WF_ROOT, WF_CHILD])
        self.assertEqual(params[2], [TASK_ID])

    def test_audit_timeline_flags_truncation(self):
        cursor = _evidence_cursor(audit_rows=[{"id": f"a{i}"} for i in range(4)])

        evidence = self.gateway.get_program_evidence(Conn(cursor), ROOT_ID, audit_limit=3)

        self.assertTrue(evidence["audit_truncated"])
        self.assertEqual(len(evidence["audit"]), 3)

    def test_export_is_audited_with_the_hash(self):
        cursor = Cursor([("FROM delivery_programs WHERE id = %s", {"id": ROOT_ID, "tenant_id": "default"})])
        conn = Conn(cursor)
        digest = "a" * 64

        self.gateway.record_program_evidence_export(
            conn, ROOT_ID, {"sha256": digest, "actor": "ops@acme.test", "tenant_id": "default"}
        )

        params = next(p for s, p in cursor.calls if "INSERT INTO session_audit_log" in s)
        self.assertEqual(params[2], "program_evidence_exported")
        self.assertEqual(json.loads(params[4])["sha256"], digest)
        self.assertEqual(conn.commits, 1)

    def test_export_audit_rejects_a_malformed_hash(self):
        cursor = Cursor([("FROM delivery_programs WHERE id = %s", {"id": ROOT_ID, "tenant_id": "default"})])

        with self.assertRaises(ValueError):
            self.gateway.record_program_evidence_export(Conn(cursor), ROOT_ID, {"sha256": "nope"})

    def test_migration_allows_the_export_action(self):
        sql = (REPO_ROOT / "database" / "migrations" / "028_audit_details_program_index.sql").read_text()
        self.assertIn("'program_evidence_exported'", sql)
        self.assertIn("(details->>'program_id')", sql)
        self.assertIn("(details->>'workflow_id')", sql)
        migrate = (REPO_ROOT / "scripts" / "ops" / "migrate-db.sh").read_text()
        self.assertIn("028_audit_details_program_index.sql", migrate)


class EvidenceDocumentTests(unittest.TestCase):
    RAW: ClassVar[dict[str, Any]] = {
        "program": {"id": ROOT_ID, "created_at": datetime(2026, 9, 1, tzinfo=UTC)},
        "decisions": [{"id": "d1"}],
        "audit": [{"id": "a1"}],
    }
    AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    def test_hash_is_stable_and_verifiable(self):
        first = build_evidence_document(self.RAW, generated_by="ops@acme.test", generated_at=self.AT)
        second = build_evidence_document(self.RAW, generated_by="ops@acme.test", generated_at=self.AT)

        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["schema"], EVIDENCE_SCHEMA)
        self.assertEqual(first["generated_at"], "2026-09-30T12:00:00Z")
        unsigned = {k: v for k, v in first.items() if k != "sha256"}
        expected = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.assertEqual(first["sha256"], expected)

    def test_tampering_changes_the_hash(self):
        document = build_evidence_document(self.RAW, generated_by="ops@acme.test", generated_at=self.AT)
        document["decisions"][0]["id"] = "forged"

        self.assertNotEqual(evidence_sha256(document), document["sha256"])

    def test_missing_sections_default_to_empty(self):
        document = build_evidence_document({}, generated_by="x", generated_at=self.AT)

        self.assertEqual(document["children"], [])
        self.assertEqual(document["compensation"], {})
        self.assertEqual(document["program"], {})


class AdminEvidenceRouteTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.runtime.config.gateway.cors_origins = ["*"]
        self.auth = mock.Mock(
            actor_id="admin-1", email="ops@acme.test", roles=["admin"], tenant_id="default"
        )
        self.runtime.policy.evaluate_identity.side_effect = lambda identity, required: mock.Mock(
            decision="allow" if set(identity["roles"]) & set(required) else "deny",
            reason="admin or owner required",
        )
        self.memory = mock.MagicMock()
        self.memory.get_program.return_value = {"program": {"id": "prog-1", "tenant_id": "default"}}
        self.memory.get_program_evidence.return_value = {
            "program": {"id": "prog-1", "tenant_id": "default"},
            "decisions": [{"id": "d1"}],
        }
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_download_is_signed_and_audited(self):
        response = self.client.get("/admin/programs/prog-1/evidence")

        self.assertEqual(response.status_code, 200)
        document = response.json()
        digest = response.headers["x-evidence-sha256"]
        self.assertEqual(document["sha256"], digest)
        self.assertEqual(evidence_sha256(document), digest)
        self.assertEqual(document["generated_by"], "ops@acme.test")
        self.assertIn(
            'filename="program-prog-1-evidence.json"', response.headers["content-disposition"]
        )
        kwargs = self.memory.record_program_evidence_export.call_args.kwargs
        self.assertEqual(kwargs["sha256"], digest)
        self.assertEqual(kwargs["tenant_id"], "default")
        self.assertEqual(kwargs["actor"], "ops@acme.test")

    def test_non_admin_is_forbidden(self):
        self.auth.roles = ["member"]

        response = self.client.get("/admin/programs/prog-1/evidence")

        self.assertEqual(response.status_code, 403)
        self.memory.get_program_evidence.assert_not_called()
        self.memory.record_program_evidence_export.assert_not_called()

    def test_program_of_another_tenant_is_not_found(self):
        self.memory.get_program.return_value = {"program": {"id": "prog-1", "tenant_id": "other"}}

        response = self.client.get("/admin/programs/prog-1/evidence")

        self.assertEqual(response.status_code, 404)
        self.memory.get_program_evidence.assert_not_called()

    def test_export_fails_closed_when_audit_write_fails(self):
        self.memory.record_program_evidence_export.side_effect = RuntimeError("db down")

        response = self.client.get("/admin/programs/prog-1/evidence")

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("x-evidence-sha256", response.headers)


if __name__ == "__main__":
    unittest.main()
