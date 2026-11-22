"""A ticket's story: coding runs with model, phases, review and cost; gates with findings; waits and decisions."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from ts_harness.implementation import progress
from ts_harness.workflows import ticket_story as ts

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


FINDINGS = [
    {"severity": "major", "category": "correctness", "file": "src/a.ts", "line": 4, "message": "Off by one."},
    {"severity": "minor", "category": "tests", "file": "src/a.ts", "line": 9, "message": "Name the test."},
]


def tasks():
    return [
        {"id": "dev1", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "succeeded",
         "created_at": at(120), "started_at": at(119), "completed_at": at(90), "input": {}},
        {"id": "ci1", "step_name": "await_pr_checks", "agent_id": "orchestrator", "status": "succeeded", "depends_on": ["dev1"],
         "created_at": at(120), "started_at": at(90), "completed_at": at(88), "output": {}},
        {"id": "cr1", "step_name": "code_review", "agent_id": "review-agent", "status": "succeeded", "depends_on": ["ci1"],
         "created_at": at(120), "started_at": at(88), "completed_at": at(87)},
        {"id": "fin1", "step_name": "finalize_code_review", "agent_id": "orchestrator", "status": "cancelled", "depends_on": ["cr1"],
         "created_at": at(120), "started_at": at(87), "updated_at": at(40), "output": {"verdict": "block", "findings": FINDINGS}},
        {"id": "dev2", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "blocked",
         "error": "awaiting_external:coding.finished", "depends_on": ["dev1"], "created_at": at(40), "started_at": at(40),
         "input": {"_revision_source": "finalize_code_review", "coding_model": "anthropic/claude-opus-5-5",
                   "_revision_grant": {"actor": "ada@example.com", "at": at(40), "waiting_since": at(87),
                                       "guidance": "/revise PROJ-6 Fix the off by one."}}},
    ]


def jobs():
    return [
        {"id": "j1", "task_id": "dev1", "status": "succeeded", "created_at": at(119.5), "started_at": at(119), "finished_at": at(90),
         "model": "claude-sonnet-5-5",
         "result": {"ok": True, "usage": [{"model": "claude-sonnet-5-5", "cost_usd": 1.0}, {"cost_usd": 0.5}],
                    "timings": {"setup": 50, "coding": 900, "verification": 600, "review": 120, "push": 3},
                    "pre_push_review": {"status": "pass", "fixed": 1, "reviews": 2, "open_blocking": []}}},
        {"id": "j2", "task_id": "dev2", "status": "running", "created_at": at(40), "started_at": at(39), "model": "claude-opus-5-5",
         "progress": {"phase": "verification", "phase_seconds": 80, "detail": "fixing test failures (pass 1)",
                      "done": {"setup": 5, "coding": 300}}},
    ]


def story():
    memory = MagicMock()
    memory.list_coding_jobs.return_value = jobs()
    return ts.ticket_story(memory, {"id": "wf-1", "tasks": tasks()}, now=NOW)


class TicketStoryTests(unittest.TestCase):
    def test_events_tell_what_happened_in_order(self):
        events = story()["events"]
        self.assertEqual([e["title"] for e in events], [
            "Coding started", "CI passed", "Code review blocked the PR", "Waiting for you", "/revise by ada@example.com",
            "Revision 1",
        ])

    def test_a_coding_run_has_model_phases_review_and_cost(self):
        first = story()["events"][0]
        self.assertEqual((first["model"], first["seconds"], first["cost_usd"], first["outcome"]),
                         ("Sonnet", 29 * 60, 1.5, "Opened the PR"))
        self.assertEqual(first["phases"]["verification"], 600)
        self.assertEqual(first["review"]["fixed"], 1)

    def test_a_blocked_gate_lists_its_blocking_findings_at_the_time_it_blocked(self):
        gate = story()["events"][2]
        self.assertEqual(gate["at"], at(87))
        self.assertEqual(gate["subtitle"], "1 blocking finding")
        self.assertEqual([f["message"] for f in gate["findings"]], ["Off by one."])
        self.assertEqual(gate["other_findings"], 1)
        self.assertIsNone(gate["seconds"])  # its open time is the wait, shown on its own

    def test_the_wait_and_the_decision(self):
        events = story()["events"]
        wait, decision, revision = events[3], events[4], events[5]
        self.assertEqual(wait["seconds"], 47 * 60)
        self.assertEqual(decision["subtitle"], "Fix the off by one.")
        self.assertEqual(revision["subtitle"], "after /revise by ada@example.com")

    def test_the_current_run_breaks_down_into_phases(self):
        run = story()["current_run"]
        self.assertEqual((run["title"], run["model"], run["phase"], run["detail"]),
                         ("Revision 1", "Opus", "verification", "fixing test failures (pass 1)"))
        self.assertEqual(run["done"], {"setup": 5, "coding": 300})
        self.assertEqual(run["seconds"], 39 * 60)

    def test_totals_count_the_wait_for_a_person_not_code_review(self):
        totals = {t["category"]: t["seconds"] for t in story()["totals"]}
        self.assertEqual(totals["you"], 47 * 60)
        self.assertEqual(totals["review"], 60)  # the reviewer's own step only
        self.assertEqual(story()["cost_usd"], 1.5)

    def test_lost_jobs_show_as_failed_attempts(self):
        memory = MagicMock()
        lost = {"id": "j0", "task_id": "dev1", "status": "lost", "created_at": at(119.9), "started_at": at(119.8),
                "finished_at": at(119.5), "error": "coding job lost: no heartbeat for 90 seconds", "result": {}}
        memory.list_coding_jobs.return_value = [lost, *jobs()]
        events = ts.ticket_story(memory, {"id": "wf-1", "tasks": tasks()}, now=NOW)["events"]
        self.assertEqual((events[0]["title"], events[0]["status"], events[0]["outcome"]),
                         ("Coding started", "failed", "Lost: no heartbeat for 90 seconds"))
        self.assertEqual(events[1]["title"], "Coding started, attempt 2")

    def test_model_labels(self):
        self.assertEqual([ts.model_label(m) for m in ("claude-opus-5-5", "anthropic/claude-sonnet-5-5", "gpt-x", "")],
                         ["Opus", "Sonnet", "gpt-x", "default model"])


class ProgressTests(unittest.TestCase):
    def test_phases_and_details_for_the_heartbeat(self):
        progress.reset()
        self.assertIsNone(progress.snapshot())
        progress.begin("setup")
        progress.begin("coding", {"setup": 4.2})
        progress.detail("fix pass 1")
        snap = progress.snapshot()
        self.assertEqual((snap["phase"], snap["detail"], snap["done"]), ("coding", "fix pass 1", {"setup": 4.2}))
        progress.begin("verification", {"setup": 4.2, "coding": 300})
        self.assertEqual(progress.snapshot()["detail"], "")
        progress.reset()


if __name__ == "__main__":
    unittest.main()
