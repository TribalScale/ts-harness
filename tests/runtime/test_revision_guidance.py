"""Guided revisions: the same review finding coming back is diagnosed before a person is asked."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from ts_harness.implementation.coding_job import _guided_model, binding_from_task_input
from ts_harness.workflows import human_decisions, revision_guidance as rg
from ts_harness.workflows.hooks import revise_on_reject
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext

FINDING = {"severity": "major", "file": "src/settings/settings.service.ts", "line": 76,
           "message": "Read the previous value inside the save transaction; the per-instance queue does not serialize instances."}
DIAGNOSIS = {"why_attempts_failed": "Each attempt kept the read outside the transaction.",
             "approach": "Lock the row with SELECT ... FOR UPDATE inside the save transaction and read the previous value there.",
             "avoid": ["relying on the per-instance write queue"],
             "test": "Two service instances save 2 and 3 concurrently; the audit chain is 4->2->3.",
             "out_of_scope": False}


def _review(i, finding, status="cancelled"):
    return {"id": f"finalize-{i}", "step_name": "finalize_code_review", "status": status, "created_at": f"2026-01-0{i}5",
            "output": {"verdict": "block", "findings": [finding]}}


def _deliver(i, **extra):
    return {"id": f"deliver-{i}", "step_name": "deliver_work_order", "status": "succeeded", "created_at": f"2026-01-0{i}0",
            "input": {"work_order_id": "WO-1", "work_order_body": "Settings with an audit log.", **extra},
            "output": {"pr_url": "https://github.com/org/api/pull/3", "summary": f"attempt {i}"}}


class RepeatTests(unittest.TestCase):
    def test_the_same_finding_in_the_same_place_is_a_repeat(self):
        moved = {**FINDING, "line": 90, "message": "Previous value read outside the transaction."}
        rounds = rg.review_rounds([_review(1, FINDING), _review(2, moved, status="blocked")], "finalize_code_review")
        self.assertEqual(len(rounds), 2)
        self.assertEqual(rg.repeated_findings(rounds)[0]["line"], 90)
        other = {**FINDING, "file": "src/server.ts", "message": "Log the port."}
        self.assertEqual(rg.repeated_findings(rg.review_rounds([_review(1, FINDING), _review(2, other, "blocked")], "finalize_code_review")), [])
        self.assertEqual(rg.repeated_findings(rounds[:1]), [])

    def test_a_diagnosis_becomes_guidance_and_a_ready_revise_reply(self):
        rounds = rg.review_rounds([_review(1, FINDING), _review(2, FINDING, "blocked")], "finalize_code_review")
        diagnosis = rg.diagnose(rounds=rounds, attempts=["a1", "a2"], work_order="WO", files={"x.ts": "code"},
                                model_call=lambda prompt: "Here:\n```json\n" + json.dumps(DIAGNOSIS) + "\n```")
        self.assertEqual(diagnosis["approach"], DIAGNOSIS["approach"])
        text = rg.guidance_text(diagnosis, rounds)
        self.assertIn("Do this: Lock the row", text)
        self.assertIn("Do not: relying on the per-instance write queue", text)
        self.assertIn("Round 2:", text)
        self.assertTrue(rg.suggested_revise("JMT-2", diagnosis).startswith("/revise JMT-2 Lock the row"))
        self.assertIsNone(rg.diagnose(rounds=rounds, attempts=[], work_order="", files={}, model_call=lambda p: "no json"))
        self.assertIsNone(rg.diagnose(rounds=rounds, attempts=[], work_order="", files={}, model_call=lambda p: 1 / 0))


class GuidedLoopTests(unittest.TestCase):
    def _ctx(self, tasks):
        skeleton = load_skeleton("deliver_work_order")
        gate = next(t for t in tasks if t["step_name"] == "finalize_code_review" and t["status"] == "blocked")
        ctx = HookContext(workflow_id="wf-1", workflow_type="deliver_work_order", tenant_id="default", task=gate,
                          stage=skeleton.stage("finalize_code_review"), skeleton=skeleton, actor="orchestrator",
                          memory_client=MagicMock(), reject_comments="fix the settings write", tasks=tasks,
                          workflow={}, runner_event="blocked",
                          extras={"error": "barrier_blocked: code_review_blocked: 1 blocking findings"})
        ctx.memory_client.get_workflow.return_value = {"workflow": {}, "tasks": tasks}
        return ctx

    @patch("ts_harness.workflows.revision_guidance.pr_files", return_value={})
    @patch("ts_harness.workflows.revision_guidance.build_model_call", return_value=lambda prompt: json.dumps(DIAGNOSIS))
    @patch("ts_harness.workflows.revision_guidance.diagnosis_model", return_value="anthropic/claude-opus-5-5")
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_at_the_limit_a_repeated_finding_gets_one_guided_revision(self, bulk, *_mocks):
        bulk.side_effect = [[f"t{i}"] for i in range(20)]
        # The first run plus three revisions: the limit (1 + 3) is used up.
        tasks = [_deliver(1), _review(1, FINDING)]
        for i in (2, 3, 4):
            tasks += [_deliver(i, _revision_source="finalize_code_review", review_feedback="x"), _review(i, FINDING, "blocked" if i == 4 else "cancelled")]
        result = revise_on_reject.handle(self._ctx(tasks))
        self.assertEqual((result["status"], result.get("guided")), ("revising", True))
        revise = bulk.call_args_list[0].kwargs["tasks"][0]
        self.assertTrue(revise["input"]["_guided"])
        self.assertIn("Do this: Lock the row", revise["input"]["review_feedback"])
        self.assertEqual(revise["input"]["coding_model"], "anthropic/claude-opus-5-5")
        self.assertEqual(revise["input"]["_guidance"]["approach"], DIAGNOSIS["approach"])

    @patch("ts_harness.workflows.revision_guidance.diagnosis_model", return_value="anthropic/claude-opus-5-5")
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_after_a_guided_attempt_fails_a_person_gets_the_diagnosis(self, bulk, _model):
        tasks = [_deliver(1), _review(1, FINDING)]
        for i in (2, 3, 4):
            tasks += [_deliver(i, _revision_source="finalize_code_review", review_feedback="x"), _review(i, FINDING)]
        tasks += [_deliver(5, _revision_source="finalize_code_review", review_feedback="guided", _guided=True, _guidance=DIAGNOSIS),
                  _review(5, FINDING, "blocked")]
        result = revise_on_reject.handle(self._ctx(tasks))
        self.assertEqual(result["status"], "revision_limit")
        self.assertEqual(result["diagnosis"]["approach"], DIAGNOSIS["approach"])
        bulk.assert_not_called()
        text = human_decisions.revision_limit_text(label="JMT-2", cause="code review blocked", feedback="f", max_revisions=3,
                                                   decisions=["revise", "accept"], diagnosis=result["diagnosis"])
        self.assertIn("A guided revision already tried this diagnosis", text)
        self.assertIn("/revise JMT-2 Lock the row", text)

    @patch("ts_harness.workflows.revision_guidance.diagnosis_model", return_value="anthropic/claude-opus-5-5")
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_a_new_finding_takes_the_ordinary_path(self, bulk, _model):
        bulk.side_effect = [[f"t{i}"] for i in range(20)]
        other = {**FINDING, "file": "src/server.ts", "line": 3, "message": "Log the port."}
        tasks = [_deliver(1), _review(1, FINDING), _deliver(2, _revision_source="finalize_code_review", review_feedback="x"),
                 _review(2, other, "blocked")]
        result = revise_on_reject.handle(self._ctx(tasks))
        self.assertEqual(result["status"], "revising")
        self.assertNotIn("guided", result)
        self.assertNotIn("_guided", bulk.call_args_list[0].kwargs["tasks"][0]["input"])
        # Every revision runs on the stronger coding model, not only guided ones.
        self.assertEqual(bulk.call_args_list[0].kwargs["tasks"][0]["input"]["coding_model"], "anthropic/claude-opus-5-5")

    @patch.dict("os.environ", {"TS_HARNESS_REVISION_CODING_MODEL": "off"})
    @patch("ts_harness.workflows.revision_guidance.diagnosis_model", return_value="anthropic/claude-opus-5-5")
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_revisions_can_keep_the_default_model(self, bulk, _model):
        bulk.side_effect = [[f"t{i}"] for i in range(20)]
        other = {**FINDING, "file": "src/server.ts", "line": 3, "message": "Log the port."}
        tasks = [_deliver(1), _review(1, FINDING), _deliver(2, _revision_source="finalize_code_review", review_feedback="x"),
                 _review(2, other, "blocked")]
        revise_on_reject.handle(self._ctx(tasks))
        self.assertNotIn("coding_model", bulk.call_args_list[0].kwargs["tasks"][0]["input"])


class GuidedModelTests(unittest.TestCase):
    def test_the_guided_model_reaches_the_coding_job_in_the_backend_format(self):
        self.assertEqual(binding_from_task_input({"coding_model": "anthropic/claude-opus-5-5"})["coding_model"], "anthropic/claude-opus-5-5")
        self.assertEqual(_guided_model("anthropic/claude-opus-5-5", "anthropic"), "claude-opus-5-5")
        self.assertEqual(_guided_model("openai/gpt-6", "anthropic"), "")
        self.assertEqual(_guided_model("claude-opus-5-5", "anthropic"), "claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()
