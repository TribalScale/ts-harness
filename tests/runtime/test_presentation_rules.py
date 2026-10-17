"""Tests for generic specialist output presentation rules."""

from __future__ import annotations

import unittest

from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.presentation import (
    orchestrator_agent_id,
    resolve_present_prompt,
    should_present_specialist_output,
)


class PresentationRuleTests(unittest.TestCase):
    def setUp(self):
        self.prd = load_skeleton("prd_request")
        self.delivery = load_skeleton("implement_delivery")

    def test_orchestrator_agent_id_defaults_to_orchestrator(self):
        self.assertEqual(orchestrator_agent_id(self.prd), "orchestrator")

    def test_should_present_specialist_output_for_pm_agent_steps(self):
        self.assertTrue(
            should_present_specialist_output(self.prd.stage("draft_prd"), self.prd)
        )
        self.assertTrue(
            should_present_specialist_output(self.prd.stage("intake"), self.prd)
        )

    def test_should_not_present_orchestrator_gate_steps_by_default(self):
        self.assertFalse(
            should_present_specialist_output(self.prd.stage("approve_prd"), self.prd)
        )
        self.assertFalse(
            should_present_specialist_output(
                self.delivery.stage("await_human_merge"),
                self.delivery,
            )
        )

    def test_should_not_present_expand_stages_by_default(self):
        self.assertFalse(
            should_present_specialist_output(
                self.delivery.stage("implement_tickets"),
                self.delivery,
            )
        )

    def test_present_output_false_opt_out(self):
        stage = self.prd.stage("draft_prd")
        opted_out = stage.__class__(**{**stage.__dict__, "present_output": False})
        self.assertFalse(should_present_specialist_output(opted_out, self.prd))

    def test_resolve_present_prompt_uses_explicit_approval_on_draft(self):
        prompt = resolve_present_prompt(self.prd.stage("draft_prd"), self.prd, [])
        self.assertEqual(prompt, "approval")

    def test_resolve_present_prompt_uses_approval_for_draft_with_gate(self):
        tasks = [
            {"id": "t1", "step_name": "draft_prd", "status": "succeeded"},
            {
                "id": "t2",
                "step_name": "approve_prd",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "depends_on": ["draft_prd"],
            },
        ]
        prompt = resolve_present_prompt(self.prd.stage("draft_prd"), self.prd, tasks)
        self.assertEqual(prompt, "approval")

    def test_resolve_present_prompt_uses_approval_when_gate_depends_on_step_name(self):
        tasks = [
            {"id": "t1", "step_name": "draft_prd", "status": "running"},
            {
                "id": "t2",
                "step_name": "approve_prd",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "depends_on": ["draft_prd"],
            },
        ]
        prompt = resolve_present_prompt(
            self.prd.stage("draft_prd"),
            self.prd,
            tasks,
            presenting_step_name="draft_prd",
        )
        self.assertEqual(prompt, "approval")

    def test_resolve_present_prompt_uses_approval_when_gate_depends_on_uuid_but_draft_running(
        self,
    ):
        tasks = [
            {"id": "t1", "step_name": "draft_prd", "status": "running"},
            {
                "id": "t2",
                "step_name": "approve_prd",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "depends_on": ["t1"],
            },
        ]
        prompt = resolve_present_prompt(
            self.prd.stage("draft_prd"),
            self.prd,
            tasks,
            presenting_step_name="draft_prd",
        )
        self.assertEqual(prompt, "approval")

    def test_resolve_present_prompt_defaults_to_inform_for_non_artifact_step(self):
        stage = self.prd.stage("await_intake_answers")
        prompt = resolve_present_prompt(stage, self.prd, [])
        self.assertEqual(prompt, "inform")

    def test_resolve_present_prompt_defaults_to_inform(self):
        prompt = resolve_present_prompt(self.prd.stage("intake"), self.prd, [])
        self.assertEqual(prompt, "intake")

    def test_resolve_present_prompt_uses_explicit_merge_on_qa_gate(self):
        skeleton = load_skeleton("deliver_work_order")
        prompt = resolve_present_prompt(skeleton.stage("qa_gate"), skeleton, [])
        self.assertEqual(prompt, "merge")


if __name__ == "__main__":
    unittest.main()
