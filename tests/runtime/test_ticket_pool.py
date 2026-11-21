"""The ticket pool: tickets whose blockers are done run in parallel, up to the project's limit."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.jira import ticket_pool as tp

ROOT = Path(__file__).resolve().parents[2]


def _issue(key, *, done=False, blocked_by=(), repo="org/api"):
    links = [{"type": {"inward": "is blocked by", "outward": "blocks"},
              "inwardIssue": {"key": b, "fields": {"status": {"name": "To Do", "statusCategory": {"key": "new"}}}}} for b in blocked_by]
    return {"key": key, "fields": {"status": {"name": "Done" if done else "To Do", "statusCategory": {"key": "done" if done else "new"}},
                                   "issuelinks": links, "description": f"Outcome: x\n\nRepo: {repo}@dev\nProgram: p"}}


class Jira:
    def __init__(self, issues):
        self.issues = {i["key"]: i for i in issues}

    def get_issue(self, key):
        return self.issues[key]


class ReadyTests(unittest.TestCase):
    def test_ready_means_not_done_and_no_open_blocker(self):
        jira = Jira([_issue("P-2", done=True), _issue("P-3"), _issue("P-4", blocked_by=["P-3"]), _issue("P-5", repo="org/web")])
        ready, done = tp.ready_tickets(jira, ["P-2", "P-3", "P-4", "P-5"])
        self.assertEqual([r["key"] for r in ready], ["P-3", "P-5"])
        self.assertEqual(done, ["P-2"])
        self.assertEqual(ready[1]["repo"], "org/web")

    def test_idle_repositories_go_first(self):
        ready = [{"key": "P-3", "repo": "org/api"}, {"key": "P-4", "repo": "org/api"}, {"key": "P-5", "repo": "org/web"}]
        self.assertEqual([c["key"] for c in tp._choose(ready, ["org/api"], 1)], ["P-5"])
        self.assertEqual([c["key"] for c in tp._choose(ready, [], 2)], ["P-3", "P-5"])
        self.assertEqual([c["key"] for c in tp._choose(ready, [], 3)], ["P-3", "P-5", "P-4"])
        self.assertEqual(tp._choose(ready, [], 0), [])
        self.assertEqual((tp.pool_limit(None), tp.pool_limit(20), tp.pool_limit("3")), (2, 8, 3))


class FillAndRefillTests(unittest.TestCase):
    def _memory(self, tasks):
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {"id": "pool", "source_channel": "web:c:1", "requester_actor_id": "u"}, "tasks": tasks}
        return memory

    @patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child", side_effect=lambda *a, **k: "child-" + k["intake_input"]["jira_issue_key"])
    def test_fill_starts_the_ready_tickets_up_to_the_limit(self, fork):
        task = {"id": "fill", "workflow_id": "pool", "tenant_id": "default", "step_name": "fill_pool",
                "input": {"summary": "implement all P", "jira_issue_keys": ["P-3", "P-4", "P-5", "P-6"], "max_parallel_tickets": 2}}
        jira = Jira([_issue("P-3"), _issue("P-4"), _issue("P-5", repo="org/web"), _issue("P-6", blocked_by=["P-3"])])
        out = tp.fill(task, {}, memory=self._memory([task]), jira=jira)
        self.assertEqual([c["label"] for c in out["children"]], ["P-3", "P-5"])
        self.assertEqual(out["queue"], ["P-3", "P-4", "P-5", "P-6"])
        self.assertEqual(fork.call_args.kwargs["source_channel"], "web:c:1")
        self.assertEqual(fork.call_args.kwargs["intake_input"]["jira_issue_key"], "P-5")

    @patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child", side_effect=lambda *a, **k: "child-" + k["intake_input"]["jira_issue_key"])
    def test_a_finished_child_frees_its_slot(self, _fork):
        fill = {"id": "fill", "step_name": "fill_pool", "input": {"summary": "s"},
                "output": {"queue": ["P-3", "P-4", "P-5", "P-6"], "max_parallel_tickets": 2}}
        join = {"id": "join", "workflow_id": "pool", "step_name": "join_pool",
                "input": {"_wait_options": {"children": [{"workflow_id": "c3", "label": "P-3", "repo": "org/api"},
                                                         {"workflow_id": "c5", "label": "P-5", "repo": "org/web"}]}}}
        memory = self._memory([fill, join])
        ctx = MagicMock(memory_client=memory, actor="runner")
        states = {"c3": "succeeded", "c5": None}
        jira = Jira([_issue("P-3", done=True), _issue("P-4"), _issue("P-5", repo="org/web"), _issue("P-6")])
        with patch("ts_harness.workflows.work_order_waves.child_state", side_effect=lambda wf, t: states.get(wf.get("id"))), \
                patch("ts_harness.jira.ticket_pool.load_workflow_payload",
                      side_effect=lambda m, wid: ({"id": wid}, []) if wid.startswith("c") else ({}, [fill, join])):
            pinned = tp.refill(ctx, join, jira=jira)
        self.assertEqual([c["label"] for c in pinned], ["P-3", "P-5", "P-4"])
        patched = memory.patch_task_input.call_args.args[1]["_wait_options"]["children"]
        self.assertEqual(len(patched), 3)
        self.assertEqual(tp.left_behind(["P-3", "P-4", "P-5", "P-6"], pinned), ["P-6"])


class ProgramTests(unittest.TestCase):
    def test_a_parallel_queue_runs_the_pool_and_feeds_it_the_remaining_tickets(self):
        from ts_harness.workflows.program_adapters import _build_implement_inputs, _extract_pick_ticket_outputs
        from ts_harness.workflows.program_materialize import select_workflow_type

        snapshot = json.loads((ROOT / "config" / "programs" / "jira_to_pr.json").read_text())
        self.assertEqual(select_workflow_type(snapshot, "implement", {"ticket_pool": True}), "implement_jira_pool")
        self.assertEqual(select_workflow_type(snapshot, "implement", {"ticket_pool": False}), "implement_jira_ticket")
        patch_ = _extract_pick_ticket_outputs(workflow_type="pick_assigned_jira", tasks=[
            {"status": "succeeded", "output": {"jira_issue_keys": ["P-3", "P-4"], "ticket_pool": True, "max_parallel_tickets": 4}}])
        self.assertEqual((patch_["ticket_pool"], patch_["max_parallel_tickets"]), (True, 4))
        intake = _build_implement_inputs(summary="s", workflow_type="implement_jira_pool", artifacts={
            "jira_issue_keys": ["P-2", "P-3", "P-4"], "completed_jira_issue_keys": ["P-2"], "max_parallel_tickets": 4, "jira_issue_key": "P-3"})
        self.assertEqual((intake["jira_issue_keys"], intake["max_parallel_tickets"]), (["P-3", "P-4"], 4))

    def test_the_pool_workflow_loads(self):
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton("implement_jira_pool")
        self.assertEqual([s.name for s in skeleton.stages], ["fill_pool", "join_pool"])
        self.assertTrue(skeleton.stage("join_pool").wait_external["pool"])


class WaitTests(unittest.TestCase):
    def _ctx(self, join, fill):
        ctx = MagicMock(actor="runner")
        ctx.memory_client.get_workflow.return_value = {"workflow": {}, "tasks": [fill, join]}
        return ctx

    def _resume(self, children, *, states, left_queue):
        from ts_harness.workflows.hooks import wait_external

        fill = {"id": "fill", "step_name": "fill_pool", "output": {"queue": left_queue, "max_parallel_tickets": 2}}
        join = {"id": "join", "workflow_id": "pool", "step_name": "join_pool", "input": {"_wait_options": {"children": children}}}
        ctx = self._ctx(join, fill)
        aggregate = {"state": states, "pending": ["P-4"] if states == "pending" else [],
                     "output": {"prs": [], "failed_labels": [], "failed_work_order_ids": []}}
        with patch("ts_harness.jira.ticket_pool.refill", return_value=children), \
                patch("ts_harness.workflows.work_order_waves.wave_result", return_value=aggregate):
            return wait_external._resume_children(ctx, task=join, event_type="workflow.child_terminal", payload={}, pool=True), ctx

    def test_the_pool_waits_while_children_run_and_reports_tickets_it_could_not_start(self):
        children = [{"workflow_id": "c3", "label": "P-3"}, {"workflow_id": "c4", "label": "P-4"}]
        result, _ = self._resume(children, states="pending", left_queue=["P-3", "P-4"])
        self.assertEqual(result["status"], "awaiting_more_children")
        result, ctx = self._resume(children, states="succeeded", left_queue=["P-3", "P-4", "P-9"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("P-9", result["reason"])
        ctx.memory_client.block_task_api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
