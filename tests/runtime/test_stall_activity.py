"""OpenCode is not killed as stalled while sub-agents work silently, and a long run pushes
work-in-progress snapshots while it codes."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.implementation.backends import opencode as oc


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


class ActivityProbeTests(unittest.TestCase):
    def test_changes_in_the_checkout_or_session_data_count_as_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, data = Path(tmp) / "repo", Path(tmp) / "data"
            repo.mkdir()
            data.mkdir()
            _git(repo, "init", "-q")
            probe = oc.activity_probe(str(data), str(repo))
            self.assertFalse(probe())
            (repo / "a.ts").write_text("x")
            self.assertTrue(probe())
            self.assertFalse(probe())
            time.sleep(0.05)
            (data / "session.json").write_text("{}")
            os.utime(data / "session.json", (time.time() + 5, time.time() + 5))
            self.assertTrue(probe())
            os.utime(repo / "a.ts", (time.time() + 10, time.time() + 10))
            self.assertTrue(probe())


class StallTests(unittest.TestCase):
    def run_silent(self, activity):
        with tempfile.TemporaryDirectory() as tmp, patch.object(oc, "ACTIVITY_PROBE_SECONDS", 0):
            return oc.stream_opencode_process(
                ["sleep", "3"], cwd=tmp, env=dict(os.environ), log_path=Path(tmp) / "log",
                work_order_id="WO-1", stall_seconds=1.5, activity=activity,
            )

    def test_a_silent_run_with_no_activity_is_stalled(self):
        self.assertEqual(self.run_silent(lambda: False).returncode, oc.OPENCODE_STALLED_RC)

    def test_a_silent_run_with_activity_is_left_to_finish(self):
        self.assertEqual(self.run_silent(lambda: True).returncode, 0)


class PeriodicCheckpointTests(unittest.TestCase):
    def test_snapshots_are_pushed_while_opencode_runs(self):
        backend = oc.OpenCodeBackend.__new__(oc.OpenCodeBackend)
        backend.runner = subprocess.run
        calls = []
        lock = threading.Lock()

        def checkpoint():
            with lock:
                calls.append(time.monotonic())

        def slow_run(*args, **kwargs):
            time.sleep(1.2)
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(oc, "checkpoint_interval_seconds", return_value=0.3), \
                patch.object(oc, "stream_opencode_process", side_effect=slow_run):
            backend._run_opencode(["opencode"], cwd="/tmp", env={}, log_path=Path("/tmp/l"), work_order_id="WO-1",
                                  checkpoint=checkpoint)
        count = len(calls)
        time.sleep(0.5)
        self.assertGreaterEqual(count, 2)
        self.assertEqual(len(calls), count)  # stops when OpenCode exits

    def test_the_default_interval_is_fifteen_minutes(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(oc.checkpoint_interval_seconds(), 900)


if __name__ == "__main__":
    unittest.main()
