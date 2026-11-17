"""Per-workflow lock helper: reentrancy, suspension, contention and fallbacks."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from ts_harness.workflows import workflow_locks
from ts_harness.workflows.workflow_locks import WorkflowLockTimeout, suspended, workflow_lock


class FakeLocks:
    def __init__(self):
        self.owners: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.guard = threading.Lock()

    def acquire_workflow_lock(self, workflow_id, *, owner, ttl_seconds):
        with self.guard:
            self.calls.append(("acquire", workflow_id))
            if self.owners.get(workflow_id, owner) != owner:
                return False
            self.owners[workflow_id] = owner
            return True

    def release_workflow_lock(self, workflow_id, *, owner):
        with self.guard:
            self.calls.append(("release", workflow_id))
            if self.owners.get(workflow_id) == owner:
                del self.owners[workflow_id]
                return True
            return False


class WorkflowLockTests(unittest.TestCase):
    def setUp(self):
        workflow_locks._unavailable_until.clear()

    def test_holds_for_the_block_and_releases_after(self):
        client = FakeLocks()
        with workflow_lock(client, "wf-1"):
            self.assertIn("wf-1", client.owners)
        self.assertEqual(client.owners, {})

    def test_nested_entry_on_the_same_thread_does_not_lock_twice(self):
        client = FakeLocks()
        with workflow_lock(client, "wf-1"):
            with workflow_lock(client, "wf-1"):
                pass
            self.assertIn("wf-1", client.owners)
        self.assertEqual(client.calls, [("acquire", "wf-1"), ("release", "wf-1")])

    def test_releases_when_the_block_raises(self):
        client = FakeLocks()
        with self.assertRaises(ValueError):
            with workflow_lock(client, "wf-1"):
                raise ValueError("boom")
        self.assertEqual(client.owners, {})

    def test_suspended_lets_another_thread_in_and_takes_the_lock_back(self):
        client = FakeLocks()
        entered = threading.Event()

        def other():
            with workflow_lock(client, "wf-1", wait_seconds=2):
                entered.set()

        with workflow_lock(client, "wf-1"):
            with suspended(client, "wf-1"):
                thread = threading.Thread(target=other)
                thread.start()
                thread.join(3)
            self.assertTrue(entered.is_set())
            self.assertIn("wf-1", client.owners)
        self.assertEqual(client.owners, {})

    def test_times_out_when_another_owner_keeps_it(self):
        client = FakeLocks()
        client.owners["wf-1"] = "someone-else"
        with self.assertRaises(WorkflowLockTimeout):
            with workflow_lock(client, "wf-1", wait_seconds=0.1):
                self.fail("entered a held lock")

    def test_waits_for_the_holder_to_release(self):
        client = FakeLocks()
        client.owners["wf-1"] = "someone-else"
        timer = threading.Timer(0.2, lambda: client.owners.pop("wf-1"))
        timer.start()
        with workflow_lock(client, "wf-1", wait_seconds=3):
            self.assertIn("wf-1", client.owners)
        timer.join()

    def test_client_without_lock_support_runs_unlocked(self):
        with workflow_lock(object(), "wf-1"):
            pass

    def test_lock_errors_run_unlocked_and_back_off(self):
        class Broken(FakeLocks):
            def acquire_workflow_lock(self, workflow_id, *, owner, ttl_seconds):
                self.calls.append(("acquire", workflow_id))
                raise ConnectionError("memory API down")

        client = Broken()
        with workflow_lock(client, "wf-1"):
            pass
        with workflow_lock(client, "wf-2"):
            pass
        self.assertEqual(client.calls, [("acquire", "wf-1")])

    def test_can_be_disabled(self):
        client = FakeLocks()
        with patch.dict("os.environ", {"WORKFLOW_LOCKS_ENABLED": "false"}):
            with workflow_lock(client, "wf-1"):
                pass
        self.assertEqual(client.calls, [])

    def test_blank_workflow_id_is_unlocked(self):
        client = FakeLocks()
        with workflow_lock(client, ""):
            pass
        self.assertEqual(client.calls, [])


class EngineLockTests(unittest.TestCase):
    def test_engine_entry_points_hold_the_workflow_lock(self):
        from ts_harness.workflows.engine import WorkflowEngine

        class Client(FakeLocks):
            def __init__(self):
                super().__init__()
                self.held_during: list[dict[str, str]] = []

            def get_workflow(self, workflow_id):
                self.held_during.append(dict(self.owners))
                raise LookupError("stop here")

        client = Client()
        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine.client = client
        with self.assertRaises(LookupError):
            engine.cancel("wf-9", actor="alice")
        self.assertEqual(list(client.held_during[0]), ["wf-9"])
        self.assertEqual(client.owners, {})


if __name__ == "__main__":
    unittest.main()
