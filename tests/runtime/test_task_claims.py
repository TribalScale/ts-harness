"""Batch claims by lane with fair ordering, lease renewal, task owners, and stage lanes."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import workflow_definitions  # noqa: E402
from memory_routes import task_claims  # noqa: E402

from ts_harness.workflows.materialize import materialize_skeleton_tasks  # noqa: E402


def _workflow(cur, *, program_id=None, parent=None) -> str:
    cur.execute(
        "INSERT INTO workflows (workflow_type, program_id, parent_workflow_id) VALUES ('t', %s, %s) RETURNING id",
        (program_id, parent),
    )
    return str(cur.fetchone()["id"])


def _task(cur, workflow_id: str, *, lane: str = "agent", status: str = "pending", requested_by=None, team_id=None,
          approval_required=False, approval_status="not_required", depends_on=None, next_attempt_at=None,
          created_offset: int = 0) -> str:
    cur.execute(
        """
        INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, lane, status, requested_by, team_id,
                                    approval_required, approval_status, depends_on, next_attempt_at, max_retries,
                                    created_at)
        VALUES (%s, 's', 'dev-agent', %s, %s, %s, %s, %s, %s, %s, %s, 1, now() - make_interval(secs => %s))
        RETURNING id
        """,
        (workflow_id, lane, status, requested_by, team_id, approval_required, approval_status, depends_on,
         next_attempt_at, 1000 - created_offset),
    )
    return str(cur.fetchone()["id"])


@requires_pg
class ClaimBatchTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workflow_tasks")
            conn.execute("DELETE FROM workflows")
            conn.commit()

    def claim(self, lane="agent", limit=10, runner="runner-a"):
        with self.db.connect() as conn:
            return task_claims.claim_batch(conn, {"runner": runner, "lane": lane, "limit": limit, "lease_seconds": 60})["tasks"]

    def test_claims_only_its_lane_and_sets_the_lease(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            agent = _task(cur, _workflow(cur), lane="agent")
            _task(cur, _workflow(cur), lane="fast")
            conn.commit()
        claimed = self.claim("agent")
        self.assertEqual([str(t["id"]) for t in claimed], [agent])
        self.assertEqual((claimed[0]["status"], claimed[0]["locked_by"], claimed[0]["attempt"]), ("running", "runner-a", 1))
        self.assertIsNotNone(claimed[0]["lock_expires_at"])

    def test_one_task_per_workflow_and_none_when_the_workflow_is_running(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            busy = _workflow(cur)
            _task(cur, busy, status="running")
            _task(cur, busy)
            pair = _workflow(cur)
            first = _task(cur, pair, created_offset=0)
            _task(cur, pair, created_offset=1)
            conn.commit()
        claimed = self.claim()
        self.assertEqual([str(t["id"]) for t in claimed], [first])

    def test_skips_unready_tasks(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            wf = _workflow(cur)
            dep = _task(cur, wf, status="blocked")
            _task(cur, _workflow(cur), depends_on=[dep])
            _task(cur, _workflow(cur), approval_required=True, approval_status="pending")
            cur.execute("SELECT now() + interval '1 hour' AS later")
            later = cur.fetchone()["later"]
            _task(cur, _workflow(cur), next_attempt_at=later)
            conn.commit()
        self.assertEqual(self.claim(), [])

    def test_fair_order_prefers_requesters_and_teams_with_less_running(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO teams (slug, display_name) VALUES ('a', 'A'), ('b', 'B') RETURNING id")
            team_a, team_b = (str(r["id"]) for r in cur.fetchall())
            _task(cur, _workflow(cur), status="running", requested_by="alice", team_id=team_a)
            _task(cur, _workflow(cur), status="running", requested_by="carol", team_id=team_a)
            alice = _task(cur, _workflow(cur), requested_by="alice", team_id=team_a, created_offset=0)
            bob = _task(cur, _workflow(cur), requested_by="bob", team_id=team_b, created_offset=5)
            dave = _task(cur, _workflow(cur), requested_by="dave", team_id=team_a, created_offset=1)
            conn.commit()
        order = [str(t["id"]) for t in self.claim(limit=3)]
        self.assertEqual(order, [bob, dave, alice])

    def test_approved_gates_and_resumed_tasks_go_first(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            old = _task(cur, _workflow(cur), lane="fast", created_offset=0)
            gate = _task(cur, _workflow(cur), lane="fast", approval_required=True, approval_status="approved", created_offset=5)
            cur.execute("SELECT now() - interval '1 second' AS earlier")
            earlier = cur.fetchone()["earlier"]
            resumed = _task(cur, _workflow(cur), lane="fast", next_attempt_at=earlier, created_offset=6)
            conn.commit()
        order = [str(t["id"]) for t in self.claim("fast")]
        self.assertEqual(order[2], old)
        self.assertEqual(set(order[:2]), {gate, resumed})

    def test_concurrent_runners_never_claim_the_same_task(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            for _ in range(30):
                _task(cur, _workflow(cur))
            conn.commit()
        results: dict[str, list[str]] = {}

        def run(name: str) -> None:
            ids: list[str] = []
            for _ in range(10):
                ids += [str(t["id"]) for t in self.claim(limit=3, runner=name)]
            results[name] = ids

        threads = [threading.Thread(target=run, args=(f"runner-{i}",)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        claimed = [task for ids in results.values() for task in ids]
        self.assertEqual(len(claimed), 30)
        self.assertEqual(len(set(claimed)), 30)

    def test_renew_lease_only_for_the_holder(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            _task(cur, _workflow(cur))
            conn.commit()
        task_id = str(self.claim()[0]["id"])
        with self.db.connect() as conn:
            self.assertTrue(task_claims.renew_lease(conn, task_id, {"runner": "runner-a", "lease_seconds": 600})["renewed"])
            self.assertFalse(task_claims.renew_lease(conn, task_id, {"runner": "runner-b"})["renewed"])

    def test_rejects_unknown_lane_and_missing_runner(self):
        with self.db.connect() as conn:
            with self.assertRaisesRegex(ValueError, "lane"):
                task_claims.claim_batch(conn, {"runner": "r", "lane": "slow"})
            with self.assertRaisesRegex(ValueError, "runner"):
                task_claims.claim_batch(conn, {"lane": "agent"})


@requires_pg
class TaskOwnerTests(PgTestCase):
    def test_tasks_take_team_and_requester_from_the_program_through_parents(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO teams (slug, display_name) VALUES ('web', 'Web') RETURNING id")
            team = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO projects (tenant_id, team_id, key, name) VALUES ('default', %s, 'SHOP', 'Shop')", (team,))
            cur.execute(
                """
                INSERT INTO delivery_programs (summary, entry_phase, current_phase, owner_id, project_id)
                VALUES ('p', 'prd', 'prd', 'alice@acme.dev', 'SHOP') RETURNING id
                """
            )
            program = str(cur.fetchone()["id"])
            parent = _workflow(cur, program_id=program)
            child = _workflow(cur, parent=parent)
            cur.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id) VALUES (%s, 's', 'a') RETURNING team_id, requested_by, lane",
                (child,),
            )
            row = cur.fetchone()
            conn.commit()
        self.assertEqual((str(row["team_id"]), row["requested_by"], row["lane"]), (team, "alice@acme.dev", "agent"))


class StageLaneTests(unittest.TestCase):
    def stage(self, **raw):
        return workflow_definitions._load_stage({"name": "s", "kind": "task", "agent_id": "dev-agent", **raw})

    def test_defaults(self):
        self.assertEqual(workflow_definitions.stage_lane(self.stage()), "agent")
        self.assertEqual(workflow_definitions.stage_lane(self.stage(execution="coding_job")), "coding")
        self.assertEqual(workflow_definitions.stage_lane(self.stage(approval_required=True)), "fast")
        self.assertEqual(workflow_definitions.stage_lane(self.stage(barrier={"hook": "plan_release"})), "fast")
        self.assertEqual(workflow_definitions.stage_lane(self.stage(wait_external={"event_type": "x"})), "fast")

    def test_override_and_validation(self):
        self.assertEqual(workflow_definitions.stage_lane(self.stage(lane="fast")), "fast")
        with self.assertRaisesRegex(ValueError, "lane must be one of"):
            self.stage(lane="slow")

    def test_tolerates_stage_like_objects(self):
        self.assertEqual(workflow_definitions.stage_lane(SimpleNamespace(barrier={"hook": "x"})), "fast")

    def test_materialized_tasks_carry_their_lane(self):
        skeleton = workflow_definitions.load_skeleton(REPO_ROOT / "config" / "workflows" / "release_track.json")
        lanes = {body["step_name"]: body["lane"] for body in materialize_skeleton_tasks(skeleton, workflow_id="wf")}
        self.assertEqual(set(lanes.values()), {"fast"})
        deliver = workflow_definitions.load_skeleton(REPO_ROOT / "config" / "workflows" / "deliver_work_order.json")
        bodies = materialize_skeleton_tasks(deliver, workflow_id="wf")
        self.assertIn(bodies[0]["lane"], {"agent", "coding"})
        self.assertIn("fast", {body["lane"] for body in bodies})


@requires_pg
class WorkflowLockRouteTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            conn.execute("DELETE FROM workflow_locks")
            self.workflow_id = _workflow(cur)
            conn.commit()

    def acquire(self, owner, ttl=60):
        with self.db.connect() as conn:
            return task_claims.acquire_workflow_lock(conn, self.workflow_id, {"owner": owner, "ttl_seconds": ttl})

    def release(self, owner):
        with self.db.connect() as conn:
            return task_claims.release_workflow_lock(conn, self.workflow_id, {"owner": owner})

    def test_one_owner_at_a_time(self):
        self.assertTrue(self.acquire("a")["acquired"])
        held = self.acquire("b")
        self.assertEqual((held["acquired"], held["owner"]), (False, "a"))
        self.assertTrue(self.acquire("a")["acquired"])

    def test_release_only_by_the_owner(self):
        self.acquire("a")
        self.assertFalse(self.release("b")["released"])
        self.assertTrue(self.release("a")["released"])
        self.assertTrue(self.acquire("b")["acquired"])

    def test_expired_lock_can_be_taken(self):
        self.acquire("a")
        with self.db.connect() as conn:
            conn.execute("UPDATE workflow_locks SET expires_at = now() - interval '1 second'")
            conn.commit()
        self.assertTrue(self.acquire("b")["acquired"])

    def test_requires_an_owner(self):
        with self.assertRaisesRegex(ValueError, "owner is required"):
            self.acquire("")
