"""Cancelling a program or a workflow reaches every workflow below it, against real Postgres."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]


def _gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_cancel", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@requires_pg
class CancelCascadeTests(PgTestCase):
    def _tree(self):
        """program -> deploy_release -> release_track -> (grandchild), plus a finished sibling."""
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO delivery_programs (summary, entry_phase, current_phase) VALUES ('p', 'release', 'release') RETURNING id")
            program = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, program_id, status) VALUES ('deploy_release', %s, 'running') RETURNING id", (program,))
            release = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, parent_workflow_id, status) VALUES ('release_track', %s, 'running') RETURNING id", (release,))
            track = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, parent_workflow_id, status) VALUES ('deploy_step', %s, 'blocked') RETURNING id", (track,))
            grandchild = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, parent_workflow_id, status) VALUES ('release_track', %s, 'succeeded') RETURNING id", (release,))
            done = str(cur.fetchone()["id"])
            for workflow, status in ((release, "blocked"), (track, "pending"), (grandchild, "blocked"), (done, "succeeded")):
                cur.execute(
                    "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, approval_required) VALUES (%s, 's', 'orchestrator', %s, true)",
                    (workflow, status),
                )
            conn.commit()
        return program, release, track, grandchild, done

    def _statuses(self, *ids):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id::text, status FROM workflows WHERE id = ANY(%s::uuid[])", (list(ids),))
            workflows = {row["id"]: row["status"] for row in cur.fetchall()}
            cur.execute("SELECT count(*) AS open FROM workflow_tasks WHERE workflow_id = ANY(%s::uuid[]) AND status IN ('pending','running','blocked')", (list(ids),))
            return workflows, cur.fetchone()["open"]

    def test_cancelling_a_program_reaches_grandchildren(self):
        program, release, track, grandchild, done = self._tree()
        with self.db.connect() as conn:
            _gateway().cancel_delivery_program(conn, program, {"actor": "ops", "reason": "test"})
        workflows, open_tasks = self._statuses(release, track, grandchild, done)
        self.assertEqual(workflows, {release: "cancelled", track: "cancelled", grandchild: "cancelled", done: "succeeded"})
        self.assertEqual(open_tasks, 0)

    def test_cancelling_a_workflow_reaches_its_children(self):
        _program, release, track, grandchild, done = self._tree()
        with self.db.connect() as conn:
            result = _gateway().cancel_workflow_bulk(conn, release, {"actor": "ops", "reason": "test"})
        self.assertEqual(sorted(result["cancelled_descendants"]), sorted([track, grandchild]))
        workflows, _ = self._statuses(track, grandchild, done)
        self.assertEqual(workflows[done], "succeeded")


if __name__ == "__main__":
    unittest.main()
