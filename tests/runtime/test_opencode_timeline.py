"""OpenCode session timeline: model time vs tool time, read from OpenCode's SQLite database."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.implementation import coding_job
from ts_harness.implementation.opencode_timeline import summarize


def _db(root: Path) -> sqlite3.Connection:
    path = root / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table session (id text primary key, title text, time_created integer, time_updated integer);
        create table message (id text primary key, session_id text, time_created integer, time_updated integer, data text);
        create table part (id text primary key, message_id text, session_id text, time_created integer, time_updated integer, data text);
        create table credential (id text primary key, value text);
        insert into credential values ('c1', 'never-read');
        """
    )
    return con


def _fill(con: sqlite3.Connection) -> None:
    con.execute("insert into session values ('s0', 'old run', 1000, 2000)")
    con.execute("insert into session values ('s1', 'Implement WO-1', 100000, 400000)")
    msg = {"role": "assistant", "modelID": "claude-sonnet-5-5", "tokens": {"output": 500}}
    con.execute("insert into message values ('m1', 's1', 100000, 160000, ?)", (json.dumps(msg),))
    con.execute("insert into message values ('m0', 's1', 99000, 99000, ?)", (json.dumps({"role": "user"}),))
    parts = [
        ("p0", "m1", 100000, 100000, {"type": "step-start"}),
        ("p1", "m1", 104000, 114000, {"type": "reasoning"}),  # 4 s to first token, 10 s thinking
        ("p2", "m1", 114000, 314000, {"type": "tool", "tool": "bash",
                                      "state": {"input": {"command": "npm test && git push https://x-access-token:ghs_abcdefghijklmnop@github.com/a/b"}}}),
        ("p3", "m1", 314000, 316000, {"type": "tool", "tool": "read", "state": {"input": {"filePath": "src/a.ts"}}}),
        ("p4", "m1", 316000, 321000, {"type": "text"}),
    ]
    for pid, mid, a, b, data in parts:
        con.execute("insert into part values (?, ?, 's1', ?, ?, ?)", (pid, mid, a, b, json.dumps(data)))
    con.commit()


class SummarizeTests(unittest.TestCase):
    def test_splits_model_and_tool_time_and_masks_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = _db(Path(tmp))
            _fill(con)
            con.close()
            sessions = summarize(tmp, since_ms=50000)
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s["title"], "Implement WO-1")
        self.assertEqual(s["wall_s"], 300.0)
        self.assertEqual(s["model_s"], 19.0)  # 4 s waiting + 10 s reasoning + 5 s text
        self.assertEqual(s["tools_s"], 202.0)
        self.assertEqual(s["tools"], {"bash": 200.0, "read": 2.0})
        self.assertEqual((s["turns"], s["models"], s["output_tokens"]), (1, {"claude-sonnet-5-5": 1}, 500))
        self.assertEqual(s["slowest"][0]["tool"], "bash")
        self.assertIn("npm test", s["slowest"][0]["call"])
        self.assertNotIn("ghs_abcdefghijklmnop", json.dumps(sessions))
        self.assertNotIn("never-read", json.dumps(sessions))

    def test_missing_database_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(summarize(tmp), [])


class RunResultTests(unittest.TestCase):
    def test_timeline_never_fails_a_run(self):
        with patch("ts_harness.implementation.opencode_timeline.summarize", side_effect=RuntimeError("locked")):
            self.assertEqual(coding_job._opencode_timeline("/tmp/ws", 0), [])

    def test_timeline_reads_the_runs_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("ts_harness.implementation.backends.opencode.opencode_data_env", return_value={"XDG_DATA_HOME": tmp}):
            con = _db(Path(tmp))
            _fill(con)
            con.close()
            sessions = coding_job._opencode_timeline("/tmp/ws", 50000)
        self.assertEqual([s["title"] for s in sessions], ["Implement WO-1"])


if __name__ == "__main__":
    unittest.main()
