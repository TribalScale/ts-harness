"""State shared by agent-runtime replicas: tool approvals, admin rate limits, service leases."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from tests.pg_support import PgTestCase, requires_pg

from ts_harness.gateway.rate_limit import RateLimiter, SharedRateLimiter
from ts_harness.gateway.service_lease import ServiceLease
from ts_harness.tools.approvals import (
    ApprovalAlreadyDecided,
    ApprovalQueue,
    PostgresApprovalQueue,
    build_approval_queue,
    decide_and_resume,
)


def _connector(url: str):
    import psycopg

    @contextmanager
    def connect():
        with psycopg.connect(url) as conn:
            yield conn

    return connect


def _request(queue, **overrides):
    fields = {"tenant_id": "acme", "agent_id": "dev-agent", "tool_name": "gmail.send", "args": {"to": "a@b.c"}, "reason": "send"}
    return queue.request(**{**fields, **overrides})


@requires_pg
class PostgresApprovalQueueTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM tool_approvals")
            conn.commit()
        self.connect = _connector(self.db.url)

    def test_a_request_on_one_replica_is_visible_on_another(self):
        approval = _request(PostgresApprovalQueue(self.connect), workflow_id="wf", task_id="t")
        other = PostgresApprovalQueue(self.connect)
        self.assertEqual(other.get(approval.approval_id).args, {"to": "a@b.c"})
        self.assertEqual([a.approval_id for a in other.list_pending(tenant_id="acme")], [approval.approval_id])
        self.assertEqual(other.list_pending(tenant_id="other"), [])
        self.assertEqual(len(other.all()), 1)

    def test_only_the_first_decision_counts(self):
        approval = _request(PostgresApprovalQueue(self.connect))
        first = PostgresApprovalQueue(self.connect).decide_pending(approval.approval_id, approved=True, actor_id="alice", roles=["admin"])
        self.assertEqual((first.status, first.decided_by_actor_id, first.decided_by_roles), ("approved", "alice", ["admin"]))
        with self.assertRaises(ApprovalAlreadyDecided):
            PostgresApprovalQueue(self.connect).decide_pending(approval.approval_id, approved=False, actor_id="bob")

    def test_approved_request_matches_until_consumed(self):
        queue = PostgresApprovalQueue(self.connect)
        approval = _request(queue, workflow_id="wf", task_id="t")
        queue.decide(approval.approval_id, approved=True, actor_id="alice")
        match = {"tool_name": "gmail.send", "args": {"to": "a@b.c"}, "tenant_id": "acme", "agent_id": "dev-agent"}
        self.assertTrue(queue.is_approved_and_matches(approval.approval_id, **match))
        self.assertEqual([a.approval_id for a in queue.decided_for_task("wf", "t")], [approval.approval_id])
        queue.consume(approval.approval_id)
        self.assertFalse(queue.is_approved_and_matches(approval.approval_id, **match))
        self.assertEqual(queue.decided_for_task("wf", "t"), [])

    def test_unknown_ids_raise_key_error(self):
        queue = PostgresApprovalQueue(self.connect)
        with self.assertRaises(KeyError):
            queue.get("not-a-uuid")
        with self.assertRaises(KeyError):
            queue.get("00000000-0000-0000-0000-000000000000")

    def test_decide_and_resume_requeues_the_parked_task(self):
        queue = PostgresApprovalQueue(self.connect)
        approval = _request(queue, workflow_id="wf", task_id="t")
        memory = MagicMock()
        decided, resume = decide_and_resume(queue, memory, approval.approval_id, approved=True, actor_id="alice")
        self.assertEqual((decided.status, resume), ("approved", {"status": "resumed"}))
        memory.resume_tool_approval.assert_called_once_with("t", approval.approval_id, actor="alice")


class ApprovalQueueSelectionTests(unittest.TestCase):
    def test_postgres_with_a_session_database(self):
        with patch("ts_harness.agent.sessions.shared_state_connection", return_value=lambda: None):
            self.assertIsInstance(build_approval_queue(), PostgresApprovalQueue)

    def test_in_process_without_one(self):
        with patch("ts_harness.agent.sessions.shared_state_connection", return_value=None):
            queue = build_approval_queue()
        self.assertIs(type(queue), ApprovalQueue)

    def test_in_process_decide_pending_rejects_a_second_decision(self):
        queue = ApprovalQueue()
        approval = _request(queue)
        queue.decide_pending(approval.approval_id, approved=True, actor_id="alice")
        with self.assertRaises(ApprovalAlreadyDecided):
            queue.decide_pending(approval.approval_id, approved=True, actor_id="bob")


@requires_pg
class SharedRateLimiterTests(PgTestCase):
    def test_counts_across_replicas(self):
        connect = _connector(self.db.url)
        replicas = [
            SharedRateLimiter(connect, max_requests=3, window_seconds=60, fallback=RateLimiter(max_requests=3))
            for _ in range(2)
        ]
        results = [replicas[i % 2].allow("10.0.0.1") for i in range(4)]
        self.assertEqual(results, [True, True, True, False])
        self.assertTrue(replicas[0].allow("10.0.0.2"))


class SharedRateLimiterFallbackTests(unittest.TestCase):
    def test_database_errors_use_the_process_limiter(self):
        def broken():
            raise ConnectionError("down")

        limiter = SharedRateLimiter(broken, max_requests=1, window_seconds=60, fallback=RateLimiter(max_requests=1))
        self.assertEqual([limiter.allow("k"), limiter.allow("k")], [True, False])


@requires_pg
class ServiceLeaseTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM service_leases")
            conn.commit()
        self.connect = _connector(self.db.url)

    def test_one_holder_at_a_time(self):
        a = ServiceLease("scheduler", self.connect, holder="a")
        b = ServiceLease("scheduler", self.connect, holder="b")
        self.assertTrue(a.held())
        self.assertFalse(b.held())
        self.assertTrue(a.held())
        a.release()
        self.assertTrue(b.held())

    def test_expired_lease_moves(self):
        ServiceLease("scheduler", self.connect, holder="a").held()
        with self.db.connect() as conn:
            conn.execute("UPDATE service_leases SET expires_at = now() - interval '1 second'")
            conn.commit()
        self.assertTrue(ServiceLease("scheduler", self.connect, holder="b").held())


class ServiceLeaseFallbackTests(unittest.TestCase):
    def test_single_process_always_holds(self):
        self.assertTrue(ServiceLease("scheduler", None).held())

    def test_unreachable_database_does_not_hold(self):
        def broken():
            raise ConnectionError("down")

        self.assertFalse(ServiceLease("scheduler", broken).held())


class SchedulerLeaderTests(unittest.TestCase):
    def _scheduler(self, leader):
        from ts_harness.gateway.scheduler import Scheduler

        config = MagicMock()
        config.agents = []
        scheduler = Scheduler(config, dispatch=MagicMock(), leader=leader)
        scheduler.run_due_once = MagicMock(side_effect=lambda **_: scheduler._stop.set())
        return scheduler

    def test_follower_does_not_run_jobs(self):
        scheduler = self._scheduler(leader=MagicMock(side_effect=[False, False]))
        scheduler._stop.wait = MagicMock(side_effect=lambda _: scheduler._stop.set())
        scheduler._loop()
        scheduler.run_due_once.assert_not_called()

    def test_leader_runs_jobs(self):
        scheduler = self._scheduler(leader=lambda: True)
        scheduler._loop()
        scheduler.run_due_once.assert_called_once()
        self.assertIsInstance(scheduler.run_due_once.call_args.kwargs["now"], datetime)
        self.assertEqual(scheduler.run_due_once.call_args.kwargs["now"].tzinfo, UTC)


    def test_lease_holder_picks_up_cron_edits_from_another_replica(self):
        import json
        import os
        import tempfile
        from pathlib import Path

        from ts_harness.gateway.scheduler import Scheduler

        config = MagicMock()
        config.agents = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cron.json"
            path.write_text(json.dumps({"jobs": []}))
            scheduler = Scheduler(config, dispatch=MagicMock(), cron_jobs=[], cron_path=path)
            self.assertFalse(scheduler.reload_cron_file_if_changed())
            path.write_text(json.dumps({"jobs": [{"name": "nightly", "agent_id": "pm-agent", "interval_seconds": 60}]}))
            os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
            self.assertTrue(scheduler.reload_cron_file_if_changed())
            self.assertEqual([job.name for job in scheduler.jobs], ["nightly"])
            self.assertFalse(scheduler.reload_cron_file_if_changed())


if __name__ == "__main__":
    unittest.main()
