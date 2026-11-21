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
