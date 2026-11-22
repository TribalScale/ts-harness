"""The Live page and the program board: what runs now, who it waits on, and where the time went."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from ts_harness.workflows import live_view as lv

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def at(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def ticket(key="PROJ-3", *, stage="review"):
    tasks = [
        {"id": "prep", "step_name": "prepare_ticket_delivery", "agent_id": "orchestrator", "status": "succeeded",
         "created_at": at(120), "started_at": at(120), "completed_at": at(119), "input": {"jira_issue_key": key}},
        {"id": "dev1", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "succeeded", "depends_on": ["prep"],
         "created_at": at(120), "started_at": at(119), "completed_at": at(90), "output": {"pr_url": "https://github.com/o/r/pull/7"}},
        {"id": "ci1", "step_name": "await_pr_checks", "agent_id": "orchestrator", "status": "succeeded", "depends_on": ["dev1"],
         "created_at": at(120), "started_at": at(90), "completed_at": at(80)},
    ]
    if stage == "review":
        tasks.append({"id": "rev1", "step_name": "code_review", "agent_id": "review-agent", "status": "running", "depends_on": ["ci1"],
                      "created_at": at(120), "started_at": at(80)})
    if stage == "merge":
        tasks.append({"id": "merge", "step_name": "await_human_merge", "agent_id": "orchestrator", "status": "blocked",
                      "error": "awaiting_external:github.merge", "depends_on": ["ci1"], "created_at": at(120), "started_at": at(30)})
    return {"id": f"wf-{key}", "status": "running", "workflow_type": "implement_jira_ticket", "tasks": tasks}


class TimelineTests(unittest.TestCase):
    def test_each_run_has_its_duration_and_category(self):
        bars = lv.step_timeline(ticket()["tasks"], now=NOW)
        self.assertEqual([(b["step_name"], b["category"], b["seconds"]) for b in bars], [
            ("prepare_ticket_delivery", "other", 60), ("deliver_work_order", "coding", 29 * 60),
            ("await_pr_checks", "ci", 600), ("code_review", "review", 80 * 60)])
        self.assertTrue(bars[-1]["open"])

    def test_a_revision_starts_when_it_was_created_not_when_the_attempt_it_revises_ended(self):
        tasks = ticket(stage="none")["tasks"] + [
            # The decision took 40 minutes; the revision was created and claimed after it.
            {"id": "dev2", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "blocked",
             "error": "awaiting_external:coding.finished", "depends_on": ["dev1"], "created_at": at(10), "started_at": at(10)},
        ]
        bars = lv.step_timeline(tasks, now=NOW)
        self.assertEqual((bars[-1]["step_name"], bars[-1]["seconds"]), ("deliver_work_order", 600))

    def test_who_it_waits_on(self):
        memory = MagicMock()
        memory.list_coding_jobs.return_value = []
        review = lv.item_view(memory, ticket(), now=NOW)
        self.assertEqual((review["waiting_on"], review["in_step_seconds"], review["jira_issue_key"]), ("agent", 80 * 60, "PROJ-3"))
        merge = lv.item_view(memory, ticket(stage="merge"), now=NOW)
        # The merge wait started when CI finished (80 minutes ago), not at its last claim.
        self.assertEqual((merge["waiting_on"], merge["in_step_seconds"]), ("you", 80 * 60))
        self.assertEqual(lv.waiting_on({"kind": "waiting", "current_step": "await_pr_checks", "state": "Waiting for PR checks"}), "ci")
        self.assertEqual(lv.waiting_on({"kind": "stuck", "current_step": "finalize_code_review"}), "you")


class BoardTests(unittest.TestCase):
    def test_columns_blockers_and_totals(self):
        memory = MagicMock()
        memory.list_coding_jobs.return_value = []
        memory.get_program.return_value = {
            "program": {"metadata": {"artifacts": {"jira_issue_keys": ["PROJ-2", "PROJ-3", "PROJ-4", "PROJ-5"],
                                                   "completed_jira_issue_keys": ["PROJ-2"], "max_parallel_tickets": 2}}},
            "workflows": [{"id": "wf-PROJ-3", "workflow_type": "implement_jira_ticket", "status": "running", "created_at": at(120)}],
        }
        memory.get_workflow_children.return_value = []
        record = ticket(stage="merge")
        memory.get_workflow.return_value = {"workflow": {k: v for k, v in record.items() if k != "tasks"}, "tasks": record["tasks"]}
        jira = MagicMock()

        def issue(key):
            links = [{"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "PROJ-3", "fields": {"status": {"name": "To Do"}}}}] if key == "PROJ-4" else []
            return {"fields": {"summary": f"title {key}", "issuelinks": links,
                               "status": {"statusCategory": {"key": "done" if key == "PROJ-2" else "new"}}}}

        jira.get_issue.side_effect = issue
        board = lv.program_board(memory, "prog-1", jira=jira, now=NOW)
        columns = {item["key"]: item["column"] for item in board["items"]}
        self.assertEqual(columns, {"PROJ-2": "done", "PROJ-3": "waiting_for_you", "PROJ-4": "waiting_on_blockers", "PROJ-5": "ready"})
        self.assertEqual(board["items"][2]["blocked_by"], ["PROJ-3"])
        self.assertEqual(board["parallel_limit"], 2)
        shares = {t["category"]: t["seconds"] for t in board["totals"]}
        self.assertEqual(shares["merge"], 80 * 60)
        self.assertEqual(shares["coding"], 29 * 60)


class LiveTests(unittest.TestCase):
    def test_running_items_across_programs_waiting_for_you_first(self):
        memory = MagicMock()
        memory.list_programs.return_value = {"programs": [{"id": "p1", "display_name": "Alpha"}, {"id": "p2", "display_name": "Hidden"}]}
        memory.get_program.return_value = {"workflows": [{"id": "wf-A", "workflow_type": "implement_jira_ticket", "status": "running"},
                                                         {"id": "wf-B", "workflow_type": "implement_jira_ticket", "status": "running"}]}
        memory.get_workflow_children.return_value = []
        records = {"wf-A": ticket("PROJ-3"), "wf-B": ticket("PROJ-4", stage="merge")}
        memory.get_workflow.side_effect = lambda wid: {"workflow": {"id": wid, "status": "running", "workflow_type": "implement_jira_ticket"},
                                                       "tasks": records[wid]["tasks"]}
        memory.list_coding_jobs.return_value = [{"status": "running"}, {"status": "queued"}]
        out = lv.live(memory, allowed=lambda pid: pid == "p1", now=NOW)
        self.assertEqual([(i["jira_issue_key"], i["waiting_on"], i["program_label"]) for i in out["items"]],
                         [("PROJ-4", "you", "Alpha"), ("PROJ-3", "agent", "Alpha")])
        self.assertEqual((out["capacity"]["running"], out["capacity"]["queued"]), (1, 1))


if __name__ == "__main__":
    unittest.main()


class HistoryTests(unittest.TestCase):
    def _record(self, key, *, status, tasks_extra=(), reason=""):
        base = ticket(key, stage="merge")
        tasks = [dict(t) for t in base["tasks"]]
        for t in tasks:
            if t["status"] in {"running", "blocked"}:
                t["status"], t["completed_at"], t["error"] = "succeeded", at(10), ""
        if status == "cancelled":
            tasks[-1]["status"] = "cancelled"  # cancelled before the merge
        tasks += list(tasks_extra)
        return {"workflow": {"id": base["id"], "status": status, "workflow_type": "implement_jira_ticket",
                             "completed_at": at(10), "cancellation_reason": reason}, "tasks": tasks}

    def test_outcomes(self):
        merged = self._record("PROJ-3", status="succeeded")
        merged["tasks"][-1]["output"] = {"external_event": {"merged_by": "dana"}}
        self.assertEqual(lv.outcome(merged["workflow"], merged["tasks"])["outcome"], "merged")
        self.assertEqual(lv.outcome(merged["workflow"], merged["tasks"])["decided_by"], "dana")
        stopped = {"id": "s", "status": "running"}
        self.assertEqual(lv.outcome(stopped, [{"step_name": "qa_gate", "error": "stopped_by_human: ana@example.com: x"}]),
                         {"outcome": "stopped", "decided_by": "ana@example.com", "finished_at": ""})
        accepted = [{"step_name": "finalize_code_review", "status": "succeeded", "input": {"_human_accept": {"actor": "harness-triage"}}}]
        self.assertEqual(lv.outcome({"status": "succeeded"}, accepted)["decided_by"], "harness")
        self.assertEqual(lv.outcome({"status": "cancelled", "cancellation_reason": "cancelled by dana"}, [])["decided_by"], "dana")
        self.assertEqual(lv.outcome({"status": "superseded"}, [])["outcome"], "replaced")

    def test_history_lists_finished_runs_with_filters_and_search(self):
        memory = MagicMock()
        memory.list_coding_jobs.return_value = []
        memory.list_programs.return_value = {"programs": [{"id": "p1", "display_name": "Alpha", "project_id": "PAY", "updated_at": at(5)}]}
        memory.get_program.return_value = {"workflows": [
            {"id": "wf-PROJ-3", "workflow_type": "implement_jira_ticket", "status": "succeeded"},
            {"id": "wf-PROJ-4", "workflow_type": "implement_jira_ticket", "status": "cancelled"},
            {"id": "wf-PROJ-5", "workflow_type": "implement_jira_ticket", "status": "running"}]}
        memory.get_workflow_children.return_value = []
        records = {"wf-PROJ-3": self._record("PROJ-3", status="succeeded"),
                   "wf-PROJ-4": self._record("PROJ-4", status="cancelled", reason="cancelled by dana")}
        memory.get_workflow.side_effect = lambda wid: records[wid]
        out = lv.history(memory, now=NOW)
        # A finished run's total stops at its last step (10 minutes ago), not now.
        self.assertEqual(out["items"][0]["elapsed_seconds"], (120 - 10) * 60)
        self.assertEqual(sorted((i["jira_issue_key"], i["outcome"]) for i in out["items"]), [("PROJ-3", "merged"), ("PROJ-4", "cancelled")])
        self.assertEqual(out["facets"]["projects"], ["PAY"])
        self.assertEqual(out["facets"]["repos"], ["o/r"])
        self.assertEqual([i["jira_issue_key"] for i in lv.history(memory, outcome_filter="cancelled", now=NOW)["items"]], ["PROJ-4"])
        self.assertEqual([i["jira_issue_key"] for i in lv.history(memory, q="proj-3", now=NOW)["items"]], ["PROJ-3"])
        self.assertEqual(lv.history(memory, project="OTHER", now=NOW)["items"], [])

    def test_the_board_keeps_earlier_attempts(self):
        memory = MagicMock()
        memory.list_coding_jobs.return_value = []
        memory.get_program.return_value = {"program": {"metadata": {"artifacts": {"jira_issue_keys": ["PROJ-3"]}}},
                                           "workflows": [{"id": "old", "workflow_type": "implement_jira_ticket", "status": "cancelled", "created_at": at(200)},
                                                         {"id": "new", "workflow_type": "implement_jira_ticket", "status": "running", "created_at": at(100)}]}
        memory.get_workflow_children.return_value = []
        old = self._record("PROJ-3", status="cancelled")
        new = ticket("PROJ-3")
        memory.get_workflow.side_effect = lambda wid: old if wid == "old" else {"workflow": {"id": "new", "status": "running"}, "tasks": new["tasks"]}
        board = lv.program_board(memory, "p1", jira=MagicMock(get_issue=lambda key: {}), now=NOW)
        entry = board["items"][0]
        self.assertEqual(entry["item"]["workflow_id"], "new")
        self.assertEqual([a["outcome"] for a in entry["attempts"]], ["cancelled"])
