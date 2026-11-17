"""scripts/close-ended-programs.py: dry run writes nothing; apply closes where it stopped.

The selection SQL was checked against Postgres with the repo migrations; these tests
cover what the script does with the rows it selects.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "close-ended-programs.py"


def load_script():
    spec = importlib.util.spec_from_file_location("close_ended_programs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


ROW = {
    "id": "prog-1",
    "tenant_id": "acme",
    "current_phase": "prd",
    "summary": "Login page",
    "metadata": json.dumps({"artifacts": {"approved_prd": {"title": "Login"}}}),
    "last_activity": "2026-11-01",
}


class CloseEndedProgramsTests(unittest.TestCase):
    def setUp(self):
        self.script = load_script()
        self.gateway = mock.Mock()

    def test_dry_run_selects_without_writing(self):
        cursor = Cursor([dict(ROW)])
        conn = Conn(cursor)
        report = self.script.close_ended_programs(conn, self.gateway, idle_days=7, apply=False, actor="t")
        self.assertEqual(report["count"], 1)
        self.assertFalse(report["applied"])
        self.assertEqual(len(cursor.calls), 1)
        self.assertEqual(cursor.calls[0][1], (["prd", "jira"], 7))
        self.assertFalse(conn.committed)
        self.gateway.audit_workflow_event.assert_not_called()

    def test_apply_marks_succeeded_where_it_stopped_and_keeps_artifacts(self):
        cursor = Cursor([dict(ROW)])
        conn = Conn(cursor)
        report = self.script.close_ended_programs(conn, self.gateway, idle_days=7, apply=True, actor="ops")
        self.assertEqual(report["count"], 1)
        update_sql, params = cursor.calls[1]
        self.assertIn("SET status = 'succeeded'", update_sql)
        self.assertIn("AND status = 'running'", update_sql)
        artifacts = json.loads(params[0])["artifacts"]
        self.assertEqual(artifacts["ended_at_phase"], "prd")
        self.assertEqual(artifacts["program_status"], "succeeded")
        self.assertTrue(artifacts["closed_by_backfill"])
        self.assertEqual(artifacts["approved_prd"], {"title": "Login"})
        self.assertEqual(self.gateway.audit_workflow_event.call_args.args[1], "delivery_program_closed_by_backfill")
        self.assertEqual(self.gateway.audit_workflow_event.call_args.args[4], "ops")
        self.assertTrue(conn.committed)

    def test_negative_idle_days_is_a_usage_error(self):
        with mock.patch("builtins.print"):
            self.assertEqual(self.script.main(["--idle-days", "-1"]), 2)


if __name__ == "__main__":
    unittest.main()
