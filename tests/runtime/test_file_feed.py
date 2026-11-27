"""The live feed of file changes: the worker's watcher, the events table and its routes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg
from ts_harness.implementation import progress
from ts_harness.implementation.file_feed import MAX_EXCERPT_LINES, FileFeed, diff_excerpt, mask_secrets

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from memory_routes import coding_job_events  # noqa: E402
from memory_routes import data_retention  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "dev")
        (self.repo / "src").mkdir()
        (self.repo / "src/app.ts").write_text("export const a = 1\n")
        (self.repo / "README.md").write_text("# App\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
        self.sent: list[dict] = []
        self.feed = FileFeed(self.repo, self.sent.extend, step=lambda: "coding", poll_seconds=3600)
        self.feed.start()

    def tearDown(self):
        self.feed._stop.set()
        self.tmp.cleanup()

    def kinds(self, events):
        return {(e["kind"], e["path"]) for e in events}

    def test_created_edited_and_deleted(self):
        (self.repo / "src/new.ts").write_text("export const b = 2\n")
        (self.repo / "src/app.ts").write_text("export const a = 1\nexport const c = 3\n")
        (self.repo / "README.md").unlink()
        events = self.feed.poll()
        self.assertEqual(self.kinds(events), {("created", "src/new.ts"), ("edited", "src/app.ts"), ("deleted", "README.md")})
        edited = next(e for e in events if e["kind"] == "edited")
        self.assertEqual((edited["additions"], edited["deletions"]), (1, 0))
        self.assertIn("+export const c = 3", edited["excerpt"])
        self.assertEqual(edited["step"], "coding")
        self.assertEqual(edited["actor"], "agent")
        self.assertEqual(self.sent, events)

    def test_a_second_edit_is_a_new_event_and_nothing_twice(self):
        (self.repo / "src/app.ts").write_text("export const a = 2\n")
        self.assertEqual(len(self.feed.poll()), 1)
        self.assertEqual(self.feed.poll(), [])
        (self.repo / "src/app.ts").write_text("export const a = 3\n")
        second = self.feed.poll()
        self.assertEqual(self.kinds(second), {("edited", "src/app.ts")})
        self.assertIn("-export const a = 2", second[0]["excerpt"])

    def test_back_to_the_base_is_restored_and_a_rename_is_one_event(self):
        (self.repo / "src/app.ts").write_text("changed\n")
        self.feed.poll()
        (self.repo / "src/app.ts").write_text("export const a = 1\n")
        self.assertEqual(self.kinds(self.feed.poll()), {("restored", "src/app.ts")})
        (self.repo / "README.md").rename(self.repo / "GUIDE.md")
        renamed = self.feed.poll()
        self.assertEqual([(e["kind"], e["path"], e.get("from_path")) for e in renamed], [("renamed", "GUIDE.md", "README.md")])

    def test_changes_inside_harness_are_the_harness(self):
        with self.feed.harness():
            (self.repo / "src/app.ts").write_text("reverted by the harness\n")
        self.assertEqual(self.sent[-1]["actor"], "harness")

    def test_many_files_in_one_folder_are_grouped(self):
        (self.repo / "docs").mkdir()
        for index in range(25):
            (self.repo / f"docs/page{index}.md").write_text(f"page {index}\n")
        events = self.feed.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["kind"], events[0]["bulk_kind"], events[0]["count"], events[0]["path"]), ("bulk", "created", 25, "docs"))

    def test_ignored_folders_are_not_reported(self):
        (self.repo / "node_modules/pkg").mkdir(parents=True)
        (self.repo / "node_modules/pkg/index.js").write_text("x\n")
        self.assertEqual(self.feed.poll(), [])


class ExcerptTests(unittest.TestCase):
    def test_excerpt_is_capped_and_counted(self):
        before = b"".join(f"line {i}\n".encode() for i in range(400))
        after = b"".join(f"row {i}\n".encode() for i in range(400))
        out = diff_excerpt(before, after, "big.txt")
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["excerpt"].splitlines()), MAX_EXCERPT_LINES)
        self.assertEqual((out["additions"], out["deletions"]), (400, 400))

    def test_binary_files_get_no_excerpt(self):
        self.assertTrue(diff_excerpt(b"\x00\x01", b"\x00\x02", "logo.png")["binary"])

    def test_secrets_are_masked(self):
        fake = "abcd" * 4  # built at runtime: a literal looks like a real key to the secret scanner
        text, masked = mask_secrets(f'API_KEY="{fake}"\ntoken: ghp_' + "a" * 36 + "\n")
        self.assertTrue(masked)
        self.assertNotIn(fake, text)
        self.assertNotIn("ghp_" + "a" * 36, text)
        self.assertIn('API_KEY="***', text)


class QueueTests(unittest.TestCase):
    def test_events_queue_and_return(self):
        progress.reset()
        progress.emit_events([{"kind": "edited", "path": "a"}, {"kind": "edited", "path": "b"}])
        batch = progress.take_events(1)
        self.assertEqual([e["path"] for e in batch], ["a"])
        progress.return_events(batch)
        self.assertEqual([e["path"] for e in progress.take_events(10)], ["a", "b"])
        self.assertEqual(progress.take_events(10), [])


@requires_pg
class EventsPostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM coding_job_events")
            conn.execute("DELETE FROM coding_jobs")
            conn.commit()

    def job(self, conn, status="running", program=None):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO coding_jobs (work_order_id, repo_url, status, spec, program_id) "
                "VALUES ('PROJ-7', 'https://github.com/org/app', %s, '{}'::jsonb, %s) RETURNING id",
                (status, program),
            )
            jid = str(cur.fetchone()["id"])
        conn.commit()
        return jid

    def events(self):
        return {
            "events": [
                {"kind": "edited", "path": "src/a.ts", "at": 1_800_000_000, "step": "coding", "additions": 2, "deletions": 1,
                 "excerpt": "@@\n-a\n+b\n+c\n"},
                {"kind": "bulk", "bulk_kind": "edited", "path": "docs", "paths": ["docs/x.md", "docs/a.ts"], "count": 68,
                 "at": 1_800_000_010, "step": "verification · fixing", "actor": "harness"},
                {"kind": "nonsense", "path": "x"},
            ]
        }

    def test_store_list_and_read_with_history(self):
        with self.db.connect() as conn:
            jid = self.job(conn)
            self.assertEqual(coding_job_events.record_events(conn, jid, self.events())["stored"], 2)
            listed = coding_job_events.list_job_events(conn, jid, {})
            self.assertEqual([e["kind"] for e in listed["events"]], ["edited", "bulk"])
            self.assertEqual(listed["events"][1]["file_count"], 68)
            self.assertEqual(listed["events"][1]["actor"], "harness")
            self.assertEqual(listed["events"][0]["work_order_id"], "PROJ-7")
            again = coding_job_events.list_job_events(conn, jid, {"after": listed["next_after"]})
            self.assertEqual(again["events"], [])
            detail = coding_job_events.get_event(conn, str(listed["events"][0]["id"]))
            self.assertIn("+b", detail["event"]["excerpt"])
            self.assertEqual([e["kind"] for e in detail["history"]], ["edited"])

    def test_a_finished_job_stores_nothing_and_recent_filters_by_program(self):
        with self.db.connect() as conn:
            done = self.job(conn, status="succeeded")
            self.assertEqual(coding_job_events.record_events(conn, done, self.events())["stored"], 0)
            program = "11111111-1111-1111-1111-111111111111"
            running = self.job(conn, program=program)
            coding_job_events.record_events(conn, running, self.events())
            mine = coding_job_events.list_recent_events(conn, {"program_ids": program, "active": "1"})
            self.assertEqual(len(mine["events"]), 2)
            none = coding_job_events.list_recent_events(conn, {"program_ids": "22222222-2222-2222-2222-222222222222"})
            self.assertEqual(none["events"], [])

    def test_retention_deletes_old_events(self):
        with self.db.connect() as conn:
            jid = self.job(conn)
            coding_job_events.record_events(conn, jid, self.events())
            conn.execute("UPDATE coding_job_events SET created_at = now() - interval '40 days'")
            conn.commit()
            with mock.patch.dict(os.environ, {}):
                out = data_retention.apply_retention(conn, {"coding_log_days": 30})
            self.assertEqual(out["coding_job_events"], {"deleted": 2})


if __name__ == "__main__":
    unittest.main()
