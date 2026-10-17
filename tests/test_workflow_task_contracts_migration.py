# flake8: noqa
"""Migration + backfill coverage for workflow task contracts (Phase 1)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database" / "migrations" / "011_workflow_task_contracts.sql"
BACKFILL = REPO_ROOT / "scripts" / "backfill-workflow-task-contracts.py"
WORKFLOW_DIR = REPO_ROOT / "config" / "workflows"


def load_backfill():
    spec = importlib.util.spec_from_file_location("backfill_contracts", BACKFILL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self, select_batches):
        self.select_batches = [list(batch) for batch in select_batches]
        self.updates = []
        self._current = []

    def execute(self, sql, params=None):
        upper = sql.upper()
        if "UPDATE" in upper:
            self.updates.append((sql, params))
        elif "SELECT" in upper:
            self._current = self.select_batches.pop(0) if self.select_batches else []

    def fetchall(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


class MigrationTests(unittest.TestCase):
    def test_migration_adds_contract_and_definition_version(self):
        sql = MIGRATION.read_text()
        self.assertIn("ADD COLUMN IF NOT EXISTS contract JSONB NOT NULL DEFAULT '{}'", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS definition_version TEXT", sql)
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_backfill()
        self.skeletons = self.mod.load_skeletons(WORKFLOW_DIR)

    def test_report_mode_flags_uncontracted_candidate(self):
        deliver = {
            "id": "t-deliver",
            "workflow_type": "deliver_work_order",
            "step_name": "deliver_work_order",
            "status": "pending",
            "contract": {},
        }
        wait = {
            "id": "t-wait",
            "workflow_type": "deliver_work_order",
            "step_name": "await_human_merge",
            "status": "pending",
            "contract": {},
        }
        cursor = FakeCursor([[deliver, wait]])
        conn = FakeConn(cursor)

        report = self.mod.backfill(conn, self.skeletons, apply=False)

        self.assertEqual(report["scanned"], 2)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(report["remaining"], ["t-deliver"])
        self.assertFalse(report["clean"])
        self.assertFalse(conn.committed)

    def test_apply_mode_backfills_and_is_clean(self):
        deliver = {
            "id": "t-deliver",
            "workflow_type": "deliver_work_order",
            "step_name": "deliver_work_order",
            "status": "pending",
            "contract": {},
        }
        # First scan returns the candidate; re-scan after apply returns nothing.
        cursor = FakeCursor([[deliver], []])
        conn = FakeConn(cursor)

        report = self.mod.backfill(conn, self.skeletons, apply=True)

        self.assertEqual(report["updated"], 1)
        self.assertEqual(report["remaining"], [])
        self.assertTrue(report["clean"])
        self.assertTrue(conn.committed)
        self.assertEqual(len(cursor.updates), 1)
        update_sql, params = cursor.updates[0]
        self.assertIn("UPDATE workflow_tasks", update_sql)
        self.assertIn("t-deliver", params)

    def test_stage_without_contract_is_not_a_candidate(self):
        wait = {
            "id": "t-wait",
            "workflow_type": "deliver_work_order",
            "step_name": "await_human_merge",
            "status": "pending",
            "contract": {},
        }
        cursor = FakeCursor([[wait]])
        conn = FakeConn(cursor)

        report = self.mod.backfill(conn, self.skeletons, apply=False)

        self.assertEqual(report["remaining"], [])
        self.assertTrue(report["clean"])


if __name__ == "__main__":
    unittest.main()
