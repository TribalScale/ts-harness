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


class LiveLimitTests(unittest.TestCase):
    """Raising the project's parallel limit reaches a pool that is already running."""

    @patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child", side_effect=lambda *a, **k: "child-" + k["intake_input"]["jira_issue_key"])
    def test_a_raised_project_limit_starts_more_tickets(self, _fork):
        fill = {"id": "fill", "step_name": "fill_pool", "input": {"summary": "s"},
                "output": {"queue": ["P-3", "P-4", "P-5", "P-6"], "max_parallel_tickets": 2}}
        join = {"id": "join", "workflow_id": "pool", "tenant_id": "default", "step_name": "join_pool",
                "input": {"_wait_options": {"children": [{"workflow_id": "c3", "label": "P-3", "repo": "org/api"},
                                                         {"workflow_id": "c5", "label": "P-5", "repo": "org/web"}]}}}
        memory = MagicMock()
        memory.list_projects.return_value = {"projects": [{"key": "P", "max_parallel_tickets": 4}]}
        ctx = MagicMock(memory_client=memory, actor="runner")
        jira = Jira([_issue("P-3"), _issue("P-4"), _issue("P-5", repo="org/web"), _issue("P-6")])
        with patch("ts_harness.workflows.work_order_waves.child_state", return_value=None), \
                patch("ts_harness.jira.ticket_pool.load_workflow_payload",
                      side_effect=lambda m, wid: ({"id": wid}, []) if wid.startswith("c") else ({}, [fill, join])):
            pinned = tp.refill(ctx, join, jira=jira)
        # Started with 2 (both running); the project now allows 4, so two more start.
        self.assertEqual([c["label"] for c in pinned], ["P-3", "P-5", "P-4", "P-6"])

    def test_unknown_project_keeps_the_starting_limit(self):
        memory = MagicMock()
        memory.list_projects.return_value = {"projects": []}
        self.assertEqual(tp.current_limit(memory, {"tenant_id": "default"}, ["P-1"], 2), 2)
        memory.list_projects.side_effect = RuntimeError("down")
        self.assertEqual(tp.current_limit(memory, {"tenant_id": "default"}, ["P-1"], 3), 3)


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


class StartTextTests(unittest.TestCase):
    def test_one_ticket_is_not_called_parallel_and_the_waiting_ones_are_named(self):
        from ts_harness.jira.ticket_pool import start_text

        text = start_text(["PROJ-7"], {"PROJ-8": ["PROJ-7"], "PROJ-23": ["PROJ-7"], "PROJ-9": ["PROJ-8"]}, 2)
        self.assertEqual(text, "Started PROJ-7. Up to 2 tickets run at once. PROJ-8 and PROJ-23 wait on PROJ-7; "
                               "PROJ-9 waits on PROJ-8.")
        self.assertEqual(start_text(["PROJ-7", "PROJ-30"], {}, 2), "Started PROJ-7 and PROJ-30 in parallel. Up to 2 tickets run at once.")


class OtherRunsTests(unittest.TestCase):
    """A ticket another run is building is never built twice, and its finish wakes the pool."""

    def _memory(self, tasks, tickets=(), pools=()):
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {"id": "pool", "source_channel": "web:c:1"}, "tasks": tasks}
        memory.running_ticket_work.return_value = {"tickets": list(tickets), "pools": list(pools)}
        return memory

    @patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child", side_effect=lambda *a, **k: "child-" + k["intake_input"]["jira_issue_key"])
    def test_fill_leaves_a_ticket_another_run_is_building(self, _fork):
        task = {"id": "fill", "workflow_id": "pool", "tenant_id": "default", "step_name": "fill_pool",
                "input": {"summary": "implement all P", "jira_issue_keys": ["P-3", "P-4"], "max_parallel_tickets": 2}}
        memory = self._memory([task], tickets=[{"key": "P-3", "workflow_id": "elsewhere"}])
        out = tp.fill(task, {}, memory=memory, jira=Jira([_issue("P-3"), _issue("P-4", repo="org/web")]))
        self.assertEqual([c["label"] for c in out["children"]], ["P-4"])
        self.assertEqual(out["busy_elsewhere"], ["P-3"])
        self.assertIn("Left to the run already building it: P-3.", out["summary"])

    def test_the_pool_keeps_waiting_for_tickets_blocked_by_another_run(self):
        fill = {"id": "fill", "step_name": "fill_pool", "output": {"queue": ["P-3", "P-4", "P-5"]}}
        join = {"id": "join", "workflow_id": "pool", "tenant_id": "default", "step_name": "join_pool"}
        memory = self._memory([fill, join], tickets=[{"key": "P-2", "workflow_id": "elsewhere"}])
        ctx = MagicMock(memory_client=memory)
        jira = Jira([_issue("P-3", done=True), _issue("P-4", blocked_by=["P-2"]), _issue("P-5", blocked_by=["P-9"])])
        children = [{"workflow_id": "c3", "label": "P-3"}]
        self.assertEqual(tp.waiting_on_other_runs(ctx, join, children, jira=jira), ["P-4"])
        memory.running_ticket_work.return_value = {"tickets": [], "pools": []}
        self.assertEqual(tp.waiting_on_other_runs(ctx, join, children, jira=jira), [])

    def test_a_finished_ticket_wakes_the_pools_waiting_on_it(self):
        memory = self._memory([], pools=[
            {"workflow_id": "pool-a", "join_status": "blocked", "queue": ["P-2", "P-4"]},
            {"workflow_id": "pool-b", "join_status": "blocked", "queue": ["P-7"]},
            {"workflow_id": "pool-c", "join_status": "blocked", "queue": ["P-2"]},
        ])
        engine = MagicMock()
        with patch("ts_harness.workflows.engine.get_workflow_engine", return_value=engine):
            woken = tp.wake_pools(memory, "P-2", exclude="pool-c")
        self.assertEqual(woken, ["pool-a"])
        self.assertEqual(engine.resume_external.call_args.args[0], "pool-a")
        self.assertEqual(engine.resume_external.call_args.kwargs["payload"], {"ticket_finished_elsewhere": "P-2"})


class MergeWaitSlotTests(unittest.TestCase):
    """A ticket waiting for its merge frees its slot, up to a cap of waiting PRs."""

    MERGE_WAIT = [{"step_name": "await_human_merge", "status": "blocked", "error": "awaiting_external:github.merge"}]
    CODING = [{"step_name": "deliver_work_order", "status": "blocked", "error": "awaiting_external:coding.finished"}]

    def _refill(self, child_tasks, *, cap="4"):
        fill = {"id": "fill", "step_name": "fill_pool", "input": {"summary": "s"},
                "output": {"queue": ["P-3", "P-4", "P-5"], "max_parallel_tickets": 1}}
        join = {"id": "join", "workflow_id": "pool", "tenant_id": "default", "step_name": "join_pool",
                "input": {"_wait_options": {"children": [{"workflow_id": "c3", "label": "P-3", "repo": "org/api"}]}}}
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {"id": "pool"}, "tasks": [fill, join]}
        memory.running_ticket_work.return_value = {"tickets": [], "pools": []}
        ctx = MagicMock(memory_client=memory, actor="runner")
        jira = Jira([_issue("P-3"), _issue("P-4", repo="org/web"), _issue("P-5")])
        with patch("ts_harness.workflows.work_order_waves.child_state", return_value=None), \
                patch("ts_harness.jira.ticket_pool.load_workflow_payload",
                      side_effect=lambda m, wid: ({"id": wid}, child_tasks) if wid.startswith("c") else ({}, [fill, join])), \
                patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child", side_effect=lambda *a, **k: "child-" + k["intake_input"]["jira_issue_key"]), \
                patch.dict("os.environ", {"TS_HARNESS_POOL_MAX_WAITING_MERGE": cap}):
            return [c["label"] for c in tp.refill(ctx, join, jira=jira)]

    def test_a_ticket_waiting_for_its_merge_lets_the_next_one_start(self):
        self.assertEqual(self._refill(self.MERGE_WAIT), ["P-3", "P-4"])

    def test_a_ticket_still_coding_holds_its_slot(self):
        self.assertEqual(self._refill(self.CODING), ["P-3"])

    def test_no_more_start_once_the_waiting_prs_reach_the_cap(self):
        self.assertEqual(self._refill(self.MERGE_WAIT, cap="1"), ["P-3"])

    def test_waiting_for_merge(self):
        self.assertTrue(tp.waiting_for_merge(self.MERGE_WAIT))
        self.assertFalse(tp.waiting_for_merge(self.MERGE_WAIT + [{"step_name": "x", "status": "running"}]))
        self.assertFalse(tp.waiting_for_merge(self.CODING))

    def test_waiting_for_a_merge_wakes_only_a_pool_parent(self):
        engine = MagicMock()
        records = {"child": {"id": "child", "parent_workflow_id": "pool"}, "pool": {"workflow_type": "implement_jira_pool"},
                   "lone": {"id": "lone", "parent_workflow_id": "other"}, "other": {"workflow_type": "software_delivery"}}
        with patch("ts_harness.jira.ticket_pool.load_workflow_payload", side_effect=lambda m, wid: (records[wid], [])), \
                patch("ts_harness.workflows.engine.get_workflow_engine", return_value=engine):
            self.assertTrue(tp.merge_wait_started(MagicMock(), "child"))
            self.assertFalse(tp.merge_wait_started(MagicMock(), "lone"))
        self.assertEqual(engine.resume_external.call_args.kwargs["payload"], {"ready_to_merge": "child"})


class BlockGraceTests(unittest.TestCase):
    """A step blocked a moment ago (its revision may be on the way) is not a failed ticket yet."""

    def test_a_fresh_block_is_still_running_and_an_old_one_failed(self):
        from datetime import datetime, timedelta, timezone

        from ts_harness.workflows.work_order_waves import child_state

        def block(minutes_ago):
            at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
            return {"step_name": "finalize_code_review", "status": "blocked", "updated_at": at,
                    "error": "barrier_blocked: code_review_blocked: 2 blocking findings"}

        done = {"step_name": "deliver_work_order", "status": "succeeded"}
        self.assertIsNone(child_state({"status": "running"}, [done, block(1)]))
        self.assertEqual(child_state({"status": "running"}, [done, block(30)]), "failed")


class ReopenPoolTests(unittest.TestCase):
    def _memory(self, join_error, child_states):
        memory = MagicMock()
        memory.running_ticket_work.return_value = {"pools": [
            {"workflow_id": "pool", "join_task_id": "join", "join_status": "blocked", "queue": ["P-1", "P-2"]}]}
        join = {"id": "join", "step_name": "join_pool", "status": "blocked", "error": join_error,
                "input": {"_wait_options": {"children": [{"workflow_id": "c1", "label": "P-1"}]}}}
        self.states = child_states
        return memory, join

    def _run(self, join_error, state):
        memory, join = self._memory(join_error, state)
        engine = MagicMock()
        with patch("ts_harness.jira.ticket_pool.load_workflow_payload",
                   side_effect=lambda m, wid: ({}, [join]) if wid == "pool" else ({"id": wid}, [])), \
                patch("ts_harness.workflows.work_order_waves.child_state", return_value=state), \
                patch("ts_harness.workflows.engine.get_workflow_engine", return_value=engine):
            reopened = tp.reopen_pools(memory)
        return reopened, memory, engine

    def test_a_pool_that_gave_up_reopens_when_its_failed_ticket_recovered(self):
        reopened, memory, engine = self._run(
            "external_blocked:workflow.child_terminal tickets not started because a blocker was not delivered: P-2; stopped or failed: P-1",
            None)
        self.assertEqual(reopened, ["pool"])
        memory.block_task_api.assert_called_once_with("join", "awaiting_external:workflow.child_terminal", actor="workflow-runner")
        self.assertEqual(engine.resume_external.call_args.kwargs["event_type"], "workflow.child_terminal")

    def test_still_failed_or_just_waiting_pools_are_left_alone(self):
        reopened, memory, _ = self._run("external_blocked:workflow.child_terminal ...; stopped or failed: P-1", "failed")
        self.assertEqual(reopened, [])
        reopened, memory, _ = self._run("awaiting_external:workflow.child_terminal", None)
        self.assertEqual(reopened, [])
        memory.block_task_api.assert_not_called()


class LeftBehindTests(unittest.TestCase):
    """A ticket another run delivered is not left behind (the pool skipped it on purpose)."""

    def test_tickets_done_in_jira_are_not_left_behind(self):
        pinned = [{"workflow_id": "c3", "label": "P-3"}]
        jira = Jira([_issue("P-3", done=True), _issue("P-4", done=True), _issue("P-5")])
        self.assertEqual(tp.left_behind(["P-3", "P-4", "P-5"], pinned, jira=jira), ["P-5"])

    def test_every_ticket_delivered_elsewhere_leaves_nothing_behind(self):
        pinned = [{"workflow_id": "c3", "label": "P-3"}]
        jira = Jira([_issue("P-3", done=True), _issue("P-4", done=True)])
        self.assertEqual(tp.open_left_behind(["P-3", "P-4"], pinned, jira=jira), [])

    def test_without_jira_every_unstarted_ticket_counts(self):
        self.assertEqual(tp.left_behind(["P-3", "P-4"], [{"label": "P-3"}]), ["P-4"])


class WorthReopeningTests(unittest.TestCase):
    """A pool that gave up only for unstarted tickets is reopened when something changed, not every poll."""

    ERROR = "external_blocked:workflow.child_terminal tickets not started because a blocker was not delivered: P-4, P-5"

    def _join(self):
        return {"input": {"_wait_options": {"children": [{"workflow_id": "c3", "label": "P-3"}]}}}

    def test_same_open_tickets_still_waiting_is_not_reopened(self):
        jira = Jira([_issue("P-3", done=True), _issue("P-4", blocked_by=["X-1"]), _issue("P-5", blocked_by=["X-1"])])
        pool = {"queue": ["P-3", "P-4", "P-5"]}
        self.assertFalse(tp._worth_reopening(self.ERROR, pool, self._join(), jira=jira))

    def test_tickets_delivered_elsewhere_reopen_it(self):
        jira = Jira([_issue("P-3", done=True), _issue("P-4", done=True), _issue("P-5", done=True)])
        pool = {"queue": ["P-3", "P-4", "P-5"]}
        self.assertTrue(tp._worth_reopening(self.ERROR, pool, self._join(), jira=jira))

    def test_a_left_behind_ticket_that_became_ready_reopens_it(self):
        jira = Jira([_issue("P-3", done=True), _issue("P-4"), _issue("P-5", blocked_by=["X-1"])])
        pool = {"queue": ["P-3", "P-4", "P-5"]}
        self.assertTrue(tp._worth_reopening(self.ERROR, pool, self._join(), jira=jira))

    def test_a_failed_ticket_block_still_reopens_on_recovery(self):
        self.assertTrue(tp._worth_reopening(self.ERROR + "; stopped or failed: P-3", {"queue": []}, self._join()))
