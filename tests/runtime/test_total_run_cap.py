"""A ticket gets at most so many coding runs in all, whatever keeps sending it back."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ts_harness.workflows.hooks import revise_on_reject as ror


def _runs(n, budgets=("", "checks", "refresh")):
    tasks = [{"id": f"d{i}", "step_name": "deliver_work_order", "status": "succeeded", "created_at": f"{i:02d}",
              "input": {"_revision_budget": budgets[i % len(budgets)]} if i else {}} for i in range(n)]
    return tasks + [{"id": "c", "step_name": "deliver_work_order", "status": "cancelled", "created_at": "99", "input": {}}]


def _ctx(tasks, *, grant=None):
    memory = MagicMock()
    memory.get_workflow.return_value = {"workflow": {"id": "wf-1"}, "tasks": tasks}
    stage = SimpleNamespace(on_block={
        "hook": "revise_on_reject", "max_revisions": 3,
        "revise": {"step_name": "deliver_work_order", "agent_id": "dev-agent", "budget": "checks"},
        "re_gate": {"step_name": "await_pr_checks", "agent_id": "orchestrator"},
    })
    gate = {"id": "g1", "step_name": "await_pr_checks", "status": "blocked", "workflow_id": "wf-1"}
    extras = {"error": "external_blocked:github.pr_checks failure"}
    if grant:
        extras["grant"] = grant
    return SimpleNamespace(task=gate, stage=stage, runner_event="blocked", extras=extras, memory_client=memory,
                           workflow_id="wf-1", actor="workflow-runner", reject_comments="CI failed", workflow={})


class RunCapTests(unittest.TestCase):
    def test_every_kind_of_run_counts_except_cancelled(self):
        self.assertEqual(ror.coding_runs(_runs(10), "deliver_work_order"), 10)

    def test_the_cap_defaults_to_10_and_can_be_changed_or_turned_off(self):
        self.assertEqual(ror.max_coding_runs(), 10)
        with patch.dict("os.environ", {"TS_HARNESS_MAX_CODING_RUNS": "6"}):
            self.assertEqual(ror.max_coding_runs(), 6)
        with patch.dict("os.environ", {"TS_HARNESS_MAX_CODING_RUNS": "0"}):
            self.assertEqual(ror.max_coding_runs(), 0)

    def test_at_the_cap_the_next_send_back_stops_for_a_decision(self):
        result = ror.handle(_ctx(_runs(10)))
        self.assertEqual((result["status"], result["reason"], result["runs"], result["max_runs"]),
                         ("revision_limit", "run_limit", 10, 10))

    def test_a_persons_or_the_arbiters_revise_still_goes_through(self):
        # With a grant the cap is passed and the hook goes on to build the revision (this fixture
        # has no source step configured, so it stops there instead of returning run_limit).
        with self.assertRaisesRegex(ValueError, "no succeeded source task"):
            ror.handle(_ctx(_runs(10), grant={"actor": "a person"}))

    def test_below_the_cap_nothing_changes(self):
        # Below the cap the hook goes on to build a normal revision (stopping at the fixture's
        # missing source step), never returning run_limit.
        with patch.object(ror, "conflicting_gates", return_value=None), \
                patch.object(ror, "_guided_plan", return_value=None):
            with self.assertRaisesRegex(ValueError, "no succeeded source task"):
                ror.handle(_ctx(_runs(5)))
