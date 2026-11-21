"""A coding run that closes its output just before exiting is not killed, and the test helper runs as a module."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.backends.opencode import stream_opencode_process

ROOT = Path(__file__).resolve().parents[2]


class OpencodeExitTests(unittest.TestCase):
    def test_closing_output_before_exiting_keeps_the_real_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = stream_opencode_process(
                ["sh", "-c", "echo finished; exec 1>&- 2>&-; sleep 1; exit 0"],
                cwd=tmp, env={"PATH": "/usr/bin:/bin"}, log_path=Path(tmp) / "log.txt", work_order_id="WO-1",
                stall_seconds=0,
            )
            self.assertEqual(done.returncode, 0)
            failed = stream_opencode_process(
                ["sh", "-c", "echo oops; exec 1>&- 2>&-; sleep 1; exit 3"],
                cwd=tmp, env={"PATH": "/usr/bin:/bin"}, log_path=Path(tmp) / "log.txt", work_order_id="WO-1",
                stall_seconds=0,
            )
            self.assertEqual(failed.returncode, 3)


class TestServicesModuleTests(unittest.TestCase):
    def test_the_integration_helper_runs_as_a_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = subprocess.run(
                [sys.executable, "-m", "ts_harness.implementation.test_services", "integration", tmp],
                capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "runtime"), "PATH": "/usr/bin:/bin"}, timeout=60,
            )
        self.assertNotIn("NameError", run.stderr)
        self.assertIn("No integration suite", run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()


class ChecksWaitTests(unittest.TestCase):
    def test_branch_protection_harness_statuses_do_not_gate_the_checks_wait(self):
        from unittest import mock

        from ts_harness.workflows import merge_policy
        from ts_harness.workflows.merge_readiness import _required_check_names

        github = mock.Mock()
        github.required_status_contexts.return_value = [
            "harness/code-review", "harness/qa", "harness/security-scan", "harness/independent-review", "bdd", "checks", "docker"]
        policy = merge_policy.RepoPolicy(required_names=[]) if "required_names" in merge_policy.RepoPolicy.__dataclass_fields__ else None
        names = _required_check_names(policy, github=github, owner="org", repo="api", base_branch="dev")
        self.assertEqual(names, ["bdd", "checks", "docker"])
