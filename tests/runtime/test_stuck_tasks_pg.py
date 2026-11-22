"""Which blocked steps reach the Inbox and chat as decisions, against real Postgres."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]


def _gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_stuck", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@requires_pg
class StuckTasksTests(PgTestCase):
    def test_blocked_for_good_is_listed_and_waits_are_not(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO workflows (workflow_type, status) VALUES ('deliver_work_order', 'running') RETURNING id")
            workflow = str(cur.fetchone()["id"])
            errors = {
                "failed_run": "/pr_url: deliver_work_order cannot succeed when run_coding_backend failed: opencode exit -9",
                "failed_checks": "external_blocked:github.pr_checks https://github.com/o/r/pull/1:test",
                "merge_wait": "awaiting_external:github.merge",
                "tool_wait": "awaiting_tool_approval:abc",
                "stopped": "stopped_by_human: alice: no",
            }
            for step, error in errors.items():
                cur.execute(
                    "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, error) VALUES (%s, %s, 'dev-agent', 'blocked', %s)",
                    (workflow, step, error),
                )
            cur.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, error, input) "
                "VALUES (%s, 'parked', 'dev-agent', 'blocked', 'review blocked', '{\"_awaiting_human_decision\": {\"reason\": \"limit\"}}')",
                (workflow,),
            )
            conn.commit()
        with self.db.connect() as conn:
            tasks = _gateway().list_stuck_tasks(conn, {"tenant_id": "default"})["tasks"]
        self.assertEqual(sorted(task["step_name"] for task in tasks), ["failed_checks", "failed_run", "parked"])



@requires_pg
class RunningTicketWorkTests(PgTestCase):
    def test_running_deliveries_and_pools_with_their_queues(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO workflows (workflow_type, status) VALUES ('implement_jira_ticket', 'running') RETURNING id")
            ticket = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, status) VALUES ('implement_jira_ticket', 'succeeded') RETURNING id")
            finished = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, status) VALUES ('implement_jira_pool', 'running') RETURNING id")
            pool = str(cur.fetchone()["id"])
            for workflow, key in ((ticket, "PROJ-7"), (finished, "PROJ-6")):
                cur.execute(
                    "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, input) "
                    "VALUES (%s, 'prepare_ticket_delivery', 'orchestrator', 'succeeded', %s::jsonb)",
                    (workflow, '{"jira_issue_key": "%s"}' % key),
                )
            cur.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, output) "
                "VALUES (%s, 'fill_pool', 'orchestrator', 'succeeded', '{\"queue\": [\"PROJ-7\", \"PROJ-8\"]}')",
                (pool,),
            )
            cur.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status) VALUES (%s, 'join_pool', 'orchestrator', 'blocked')",
                (pool,),
            )
            conn.commit()
        with self.db.connect() as conn:
            work = _gateway().running_ticket_work(conn, {"tenant_id": "default"})
        self.assertEqual([(t["key"], t["workflow_id"]) for t in work["tickets"]], [("PROJ-7", ticket)])
        self.assertEqual(work["pools"], [{"workflow_id": pool, "join_task_id": work["pools"][0]["join_task_id"],
                                          "join_status": "blocked", "queue": ["PROJ-7", "PROJ-8"]}])


if __name__ == "__main__":
    unittest.main()
