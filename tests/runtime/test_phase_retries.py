"""Failed program phase completions are recorded, retried with backoff, and never repeated."""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from ts_harness.memory.client import MemoryApiConflict
from ts_harness.workflows import phase_retries


class FakeKv:
    def __init__(self):
        self.value, self.version = None, 0
        self.conflicts = 0

    def kv_get(self, namespace, key):
        return {"value": copy.deepcopy(self.value), "version": self.version}

    def kv_put(self, namespace, key, value, expected_version=None):
        if self.conflicts:
            self.conflicts -= 1
            self.version += 1
            raise MemoryApiConflict("stale", {"code": "version_conflict"})
        if expected_version is not None and expected_version != self.version:
            raise MemoryApiConflict("stale", {"code": "version_conflict"})
        self.value, self.version = copy.deepcopy(value), self.version + 1
        return self.version


def _engine(memory, invoke):
    engine = MagicMock()
    engine.client = memory
    engine._context.side_effect = lambda **kw: SimpleNamespace(extras={}, **kw)
    engine.registry.invoke.side_effect = invoke
    return engine


class PhaseRetryTests(unittest.TestCase):
    def setUp(self):
        self.kv = FakeKv()
        self.kv.get_workflow = lambda wid: {"workflow": {"status": "succeeded", "program_id": "p-1"}}
        self.kv.get_program = lambda pid: {"status": "running"}

    def test_first_failure_is_new_and_repeats_count_attempts(self):
        self.assertTrue(phase_retries.record_failure(self.kv, workflow_id="wf", workflow_type="t", tenant_id="d", error="boom", now=0))
        self.kv.conflicts = 1  # another writer got in first; the update is retried
        self.assertFalse(phase_retries.record_failure(self.kv, workflow_id="wf", workflow_type="t", tenant_id="d", error="again", now=10))
        self.assertEqual(self.kv.value["wf"]["attempts"], 2)

    def test_backoff_doubles_up_to_an_hour(self):
        self.assertFalse(phase_retries.due({"attempts": 1, "last_attempt_at": 0}, 299))
        self.assertTrue(phase_retries.due({"attempts": 1, "last_attempt_at": 0}, 300))
        self.assertFalse(phase_retries.due({"attempts": 2, "last_attempt_at": 0}, 599))
        self.assertTrue(phase_retries.due({"attempts": 9, "last_attempt_at": 0}, 3600))

    def test_a_successful_retry_clears_the_entry_and_tells_the_chat(self):
        phase_retries.record_failure(self.kv, workflow_id="wf", workflow_type="t", tenant_id="d", error="boom", now=0)
        engine = _engine(self.kv, lambda name, ctx: {"status": "phase_completed"})
        result = phase_retries.retry_due(engine=engine, now=1000)
        self.assertEqual(result["recovered"], ["wf"])
        self.assertEqual(self.kv.value, {})
        engine._maybe_notify.assert_called_once()

    def test_a_failing_retry_stays_queued(self):
        phase_retries.record_failure(self.kv, workflow_id="wf", workflow_type="t", tenant_id="d", error="boom", now=0)
        engine = _engine(self.kv, MagicMock(side_effect=RuntimeError("still down")))
        self.assertEqual(phase_retries.retry_due(engine=engine, now=1000)["still_failing"], ["wf"])
        self.assertEqual(self.kv.value["wf"]["attempts"], 2)

    def test_a_program_that_moved_on_is_never_completed_twice(self):
        phase_retries.record_failure(self.kv, workflow_id="wf", workflow_type="t", tenant_id="d", error="boom", now=0)
        self.kv.get_workflow = lambda wid: {"workflow": {"status": "superseded", "program_id": "p-1"}}
        engine = _engine(self.kv, MagicMock())
        phase_retries.retry_due(engine=engine, now=1000)
        engine.registry.invoke.assert_not_called()
        self.assertEqual(self.kv.value, {})

    def test_the_engine_queues_a_failed_completion_and_announces_it_once(self):
        from ts_harness.workflows.engine import WorkflowEngine

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine.client = self.kv
        engine.registry = MagicMock()
        engine.registry.invoke.side_effect = RuntimeError("embed.sh failed")
        engine._maybe_notify = MagicMock()
        ctx = SimpleNamespace(workflow={"program_id": "p-1"}, workflow_id="wf", workflow_type="work_orders_from_prd",
                              tenant_id="default", extras={})
        self.assertIsNone(engine._maybe_complete_program_phase(ctx))
        self.assertIsNone(engine._maybe_complete_program_phase(ctx))
        self.assertEqual(self.kv.value["wf"]["attempts"], 2)
        engine._maybe_notify.assert_called_once()
        self.assertIn("retried automatically", ctx.extras["channel_text"])


if __name__ == "__main__":
    unittest.main()
