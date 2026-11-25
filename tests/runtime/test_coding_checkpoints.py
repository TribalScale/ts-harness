"""A long coding run pushes work-in-progress checkpoints and stops before its time limit, so a
run that is cut off is continued instead of starting over."""

from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ts_harness.implementation.backends.opencode import OpenCodeBackend


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _backend():
    backend = OpenCodeBackend.__new__(OpenCodeBackend)
    backend.runner = subprocess.run
    return backend


class SnapshotTests(unittest.TestCase):
    def test_the_snapshot_holds_the_work_and_nothing_local_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "t")
            (repo / "a.txt").write_text("one\n")
            _git(repo, "add", "a.txt")
            _git(repo, "commit", "-q", "-m", "base")
            head = _git(repo, "rev-parse", "HEAD")
            (repo / "a.txt").write_text("two\n")
            (repo / "new.txt").write_text("new\n")
            status_before = _git(repo, "status", "--porcelain")

            sha = _backend()._snapshot_commit(repo, "WIP checkpoint WO-1: after coding")

            self.assertTrue(sha)
            self.assertEqual(_git(repo, "rev-parse", "HEAD"), head)
            self.assertEqual(_git(repo, "status", "--porcelain"), status_before)
            self.assertEqual(_git(repo, "show", f"{sha}:a.txt"), "two")
            self.assertEqual(_git(repo, "show", f"{sha}:new.txt"), "new")
            self.assertEqual(_git(repo, "rev-parse", f"{sha}^"), head)
            self.assertFalse((repo / ".git" / "ts-harness-checkpoint-index").exists())


class CheckpointTests(unittest.TestCase):
    def request(self, **extra):
        return SimpleNamespace(pr_url=extra.get("pr_url", ""), base_branch="dev", work_order_id="WO-1",
                               repo_url="https://github.com/acme/app", run_budget_seconds=extra.get("budget", 0))

    def test_a_first_run_pushes_the_snapshot_with_a_lease(self):
        calls = []

        def runner(cmd, **_):
            calls.append(cmd)
            out = "abc123\trefs/heads/wo/wo-1\n" if "ls-remote" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")

        backend = _backend()
        backend.runner = runner
        with patch("ts_harness.implementation.backends.opencode.list_changed_files", return_value=["a.ts"]), \
                patch("ts_harness.implementation.security_scan.scan_secrets", return_value=SimpleNamespace(findings=[])), \
                patch.object(OpenCodeBackend, "_snapshot_commit", return_value="def456"), \
                patch("ts_harness.implementation.backends.opencode._git_env", return_value={}):
            note = backend._checkpoint(Path("/r"), self.request(), "wo/wo-1", "after coding")
        push = next(c for c in calls if "push" in c)
        self.assertIn("--force-with-lease=refs/heads/wo/wo-1:abc123", push)
        self.assertIn("def456:refs/heads/wo/wo-1", push)
        self.assertEqual(note, "work in progress pushed to wo/wo-1")

    def test_no_checkpoint_onto_an_open_pr_or_past_a_secret(self):
        backend = _backend()
        self.assertEqual(backend._checkpoint(Path("/r"), self.request(pr_url="https://x/pull/1"), "b", "x"), "")
        with patch("ts_harness.implementation.backends.opencode.list_changed_files", return_value=["a.ts"]), \
                patch("ts_harness.implementation.security_scan.scan_secrets", return_value=SimpleNamespace(findings=[{"r": 1}])), \
                patch.object(OpenCodeBackend, "_snapshot_commit") as snap:
            self.assertIn("not pushed", backend._checkpoint(Path("/r"), self.request(), "b", "x"))
        snap.assert_not_called()

    def test_out_of_time_follows_the_budget(self):
        backend = _backend()
        backend._run_started = time.monotonic() - 100
        self.assertTrue(backend._out_of_time(self.request(budget=60)))
        self.assertFalse(backend._out_of_time(self.request(budget=600)))
        self.assertFalse(backend._out_of_time(self.request(budget=0)))

    def test_the_time_limit_stop_reports_a_resumable_error(self):
        backend = _backend()
        backend._run_started = time.monotonic() - 3 * 3600
        with patch.object(OpenCodeBackend, "_checkpoint", return_value="work in progress pushed to b"), \
                patch("ts_harness.implementation.backends.opencode.list_changed_files", return_value=["a.ts"]):
            result = backend._time_limit_result(Path("/r"), self.request(), "b", "fix pass 3", log="", log_path=Path("/l"))
        self.assertTrue(result.error.startswith("time_limit: stopped before fix pass 3 after 180 minutes"))
        self.assertIn("work in progress pushed to b", result.error)


class LimitTests(unittest.TestCase):
    def test_the_default_limit_is_four_hours_and_the_run_stops_twenty_minutes_early(self):
        from ts_harness.implementation import coding_job

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(coding_job.coding_job_max_runtime_seconds(), 14400)
            self.assertEqual(coding_job.run_budget_seconds(), 13200)
        with patch.dict("os.environ", {"CODING_JOB_MAX_RUNTIME_SECONDS": "7200"}):
            self.assertEqual(coding_job.run_budget_seconds(), 6000)


if __name__ == "__main__":
    unittest.main()
