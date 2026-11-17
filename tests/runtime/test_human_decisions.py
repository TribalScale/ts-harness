"""Human decisions on a delivery step that is stuck after the automatic revision limit."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.engine import WorkflowEngine
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.human_decisions import stuck_steps

ERROR = "barrier_blocked: code_review_blocked: 1 blocking findings"
FEEDBACK = "Blocking findings (fix every one):\n- [major/correctness] clearing the portfolio keeps the old balance"


def _deliver(i, **extra):
    return {
        "id": f"deliver-{i}",
        "workflow_id": "wf-d",
        "workflow_type": "deliver_work_order",
        "step_name": "deliver_work_order",
        "status": "succeeded",
        "input": {"work_order_id": "WO-003", **extra},
        "output": {"pr_url": "https://github.com/org/app/pull/18"},
        "created_at": f"2026-01-0{i}",
    }


def _finalize(*, flagged=True, status="blocked"):
    task_input = {"_awaiting_human_decision": {"reason": "revision_limit"}} if flagged else {}
    return {
        "id": "finalize-4",
        "workflow_id": "wf-d",
        "workflow_type": "deliver_work_order",
        "step_name": "finalize_code_review",
        "status": status,
        "error": ERROR,
        "input": task_input,
        "output": {"review_feedback": FEEDBACK, "verdict": "block"},
        "created_at": "2026-01-05",
    }


class HumanDecisionTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.engine = WorkflowEngine(client=self.client, registry=build_default_registry())
        self.notified: list[dict] = []

        def capture(ctx, event):
            self.notified.append({"event": event, **ctx.extras})
            return {"status": "notified"}

        patcher = patch.object(self.engine, "_maybe_notify", side_effect=capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        phase = patch.object(self.engine, "_maybe_complete_program_phase", return_value=None)
        self.complete_phase = phase.start()
        self.addCleanup(phase.stop)

    def _state(self, tasks):
        self.client.get_workflow.return_value = {
            "workflow": {"id": "wf-d", "workflow_type": "deliver_work_order", "source_channel": "web:abc"},
            "tasks": tasks,
        }

    def test_revision_limit_parks_the_step_for_a_human_and_says_how_to_decide(self):
        finalize = _finalize(flagged=False)
        self._state([_deliver(i) for i in range(1, 5)] + [finalize])

        result = self.engine.on_task_transition(finalize, "blocked", dispatch_result={"output": finalize["output"]}, error=ERROR)

        self.assertEqual(result["status"], "revision_limit")
        task_id, patched = self.client.patch_task_input.call_args.args
        self.assertEqual(task_id, "finalize-4")
        self.assertEqual(patched["_awaiting_human_decision"]["reason"], "revision_limit")
        text = self.notified[-1]["channel_text"]
        self.assertIn("WO-003", text)
        for command in ("/revise WO-003", "/accept WO-003", "/stop WO-003"):
            self.assertIn(command, text)
        self.assertIn("3 more automatic revisions", text)

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_revise_grants_a_fresh_budget_with_the_human_guidance(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-5"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        self._state([_deliver(i) for i in range(1, 5)] + [_finalize()])

        result = self.engine.decide_blocked_step("wf-d", "finalize-4", "revise", actor="alice", guidance="Persist the empty state.")

        self.assertEqual(result["status"], "revising")
        deliver_input = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertEqual(deliver_input["_revision_grant"]["actor"], "alice")
        self.assertIn("clearing the portfolio keeps the old balance", deliver_input["review_feedback"])
        self.assertIn("Persist the empty state.", deliver_input["review_feedback"])
        self.client.cancel_task.assert_any_call("finalize-4", actor="alice", reason="superseded by revision")
        self.assertIn("alice asked for another round on WO-003", self.notified[-1]["channel_text"])
        self.assertIn("revision 1/3", self.notified[-1]["channel_text"])

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_gateway_token_decision_reads_as_an_admin(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-5"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        self._state([_deliver(i) for i in range(1, 5)] + [_finalize()])

        self.engine.decide_blocked_step("wf-d", "finalize-4", "revise", actor="system", guidance="Wire it into App.jsx.")

        deliver_input = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertIn("Guidance from an admin:", deliver_input["review_feedback"])
        self.assertIn("An admin asked for another round on WO-003", self.notified[-1]["channel_text"])

    def test_accept_requeues_the_step_with_the_override(self):
        self._state([_deliver(1), _finalize()])

        result = self.engine.decide_blocked_step("wf-d", "finalize-4", "accept", actor="alice")

        self.assertEqual(result["status"], "accepted")
        task_id = self.client.resume_blocked_task.call_args.args[0]
        kwargs = self.client.resume_blocked_task.call_args.kwargs
        self.assertEqual(task_id, "finalize-4")
        self.assertEqual(kwargs["input_patch"]["_human_accept"]["actor"], "alice")
        self.assertIsNone(kwargs["input_patch"]["_awaiting_human_decision"])
        self.assertIn("alice accepted WO-003 as it is", self.notified[-1]["channel_text"])

    def test_stop_ends_the_work_order_and_lets_the_wave_see_it_failed(self):
        self._state([_deliver(1), _finalize()])

        result = self.engine.decide_blocked_step("wf-d", "finalize-4", "stop", actor="alice")

        self.assertEqual(result["status"], "stopped")
        task_id, patched = self.client.patch_task_input.call_args.args
        self.assertIsNone(patched["_awaiting_human_decision"])
        block_args = self.client.block_task_api.call_args
        self.assertEqual(block_args.args[0], "finalize-4")
        self.assertTrue(block_args.args[1].startswith("stopped_by_human: alice"))
        self.complete_phase.assert_called_once()
        self.assertIn("alice stopped WO-003", self.notified[-1]["channel_text"])

    def test_only_a_stuck_step_can_be_decided(self):
        self._state([_deliver(1), _finalize(status="succeeded")])
        with self.assertRaisesRegex(ValueError, "not waiting for a decision"):
            self.engine.decide_blocked_step("wf-d", "finalize-4", "accept", actor="alice")
        self._state([_deliver(1), _finalize()])
        with self.assertRaisesRegex(ValueError, "unknown decision"):
            self.engine.decide_blocked_step("wf-d", "finalize-4", "merge", actor="alice")

    def test_accepted_code_review_completes_with_its_output_and_a_passing_status(self):
        from ts_harness.workflows.human_decisions import accepted_output

        task = _finalize(flagged=False, status="running")
        task["output"]["prs"] = [{"pr_url": "https://github.com/org/app/pull/18", "head_sha": "abc123"}]
        task["input"] = {"_human_accept": {"actor": "alice", "at": "t", "output": task["output"]}}
        github = MagicMock()

        output = accepted_output(task, github=github)

        self.assertEqual(output["verdict"], "accepted")
        self.assertEqual(output["human_override"]["actor"], "alice")
        self.assertEqual(output["review_feedback"], FEEDBACK)
        args, kwargs = github.create_status.call_args
        self.assertEqual(args, ("org", "app", "abc123"))
        self.assertEqual((kwargs["state"], kwargs["context"]), ("success", "harness/code-review"))
        self.assertIn("accepted by alice", kwargs["description"])

    def test_stuck_steps_lists_parked_tasks_only(self):
        waiting = {**_finalize(flagged=False), "id": "merge-1", "error": "awaiting_external:github.merge"}
        self.assertEqual([task["id"] for task in stuck_steps([_deliver(1), _finalize(), waiting])], ["finalize-4"])


if __name__ == "__main__":
    unittest.main()
