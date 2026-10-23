"""OpenCode streaming progress log helper."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.backends.opencode import (
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


if __name__ == "__main__":
    unittest.main()
