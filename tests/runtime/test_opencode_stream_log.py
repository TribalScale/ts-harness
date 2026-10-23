"""OpenCode streaming progress log helper."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from ts_harness.implementation.backends.opencode import (
    OPENCODE_STALLED_RC,
    opencode_exit_error,
    opencode_progress_log_path,
    stream_opencode_process,
)


class OpenCodeStreamLogTests(unittest.TestCase):
    def test_progress_log_path_sanitizes_work_order_id(self):
        path = opencode_progress_log_path(Path("/ws/WO-001"), "WO-001")
        self.assertEqual(path, Path("/ws/WO-001/.ts-harness/opencode-WO-001.log"))

    def test_stream_opencode_process_writes_log_and_captures_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / ".ts-harness" / "opencode-WO-001.log"
            script = Path(tmp) / "fake_opencode.py"
            script.write_text(
                "import sys\n"
                "print('line-one')\n"
                "print('line-two')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            completed = stream_opencode_process(
                [os.environ.get("PYTHON", "python3"), str(script)],
                cwd=tmp,
                env=os.environ.copy(),
                log_path=log_path,
                work_order_id="WO-001",
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("line-one", completed.stdout)
            self.assertIn("line-two", completed.stdout)
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("opencode start work_order_id=WO-001", text)
            self.assertIn("line-one", text)
            self.assertIn("opencode end rc=0", text)

    def _script(self, tmp: str, body: str) -> list[str]:
        script = Path(tmp) / "fake_opencode.py"
        script.write_text(body, encoding="utf-8")
        return [os.environ.get("PYTHON", "python3"), str(script)]

    def test_silent_process_is_killed_with_its_children_after_the_stall_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            cmd = self._script(
                tmp,
                "import subprocess, sys, time\n"
                f"child = subprocess.Popen(['sleep', '60'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "print('loaded skills', flush=True)\n"
                "time.sleep(60)\n",
            )
            log_path = Path(tmp) / ".ts-harness" / "opencode-WO-002.log"
            started = time.monotonic()
            completed = stream_opencode_process(
                cmd,
                cwd=tmp,
                env=os.environ.copy(),
                log_path=log_path,
                work_order_id="WO-002",
                stall_seconds=1,
            )
            self.assertLess(time.monotonic() - started, 15)
            self.assertEqual(completed.returncode, OPENCODE_STALLED_RC)
            self.assertIn("loaded skills", completed.stdout)
            self.assertIn("no output for 1s", log_path.read_text(encoding="utf-8"))
            child_pid = int(pid_file.read_text())
            time.sleep(0.2)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_steady_output_is_not_a_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._script(
                tmp,
                "import time\n"
                "for i in range(8):\n"
                "    print(f'step {i}', flush=True)\n"
                "    time.sleep(0.3)\n",
            )
            completed = stream_opencode_process(
                cmd,
                cwd=tmp,
                env=os.environ.copy(),
                log_path=Path(tmp) / "log",
                work_order_id="WO-001",
                stall_seconds=1,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("step 7", completed.stdout)

    def test_stall_error_names_the_stall(self):
        stalled = subprocess.CompletedProcess(["opencode"], OPENCODE_STALLED_RC, stdout="", stderr="")
        self.assertEqual(opencode_exit_error(stalled), "opencode stalled: no output within the stall window")
        crashed = subprocess.CompletedProcess(["opencode"], 1, stdout="", stderr="")
        self.assertEqual(opencode_exit_error(crashed), "opencode exit 1")


if __name__ == "__main__":
    unittest.main()
