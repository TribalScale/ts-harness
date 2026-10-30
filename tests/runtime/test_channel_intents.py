"""Table-driven tests for channel intent classification."""

from __future__ import annotations

import unittest

from ts_harness.config.schema import load_runtime_config
from ts_harness.routing.intents import classify_channel_intent


class ChannelIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_runtime_config("config/ts-harness-runtime.test.yaml")

    def test_governed_prd_phrases(self):
        cases = [
            "Create a PRD from these requirements",
            "Draft PRD for the mobile checkout flow",
            "Write a product requirements document for SSO",
        ]
        for text in cases:
            intent = classify_channel_intent(text, {}, config=self.config)
            self.assertEqual(intent.kind, "governed_prd", msg=text)
            self.assertEqual(intent.workflow_type, "prd_request")

    def test_informal_prd_escape(self):
        intent = classify_channel_intent("Quick PRD outline only, no approval process", {}, config=self.config)
        self.assertEqual(intent.kind, "informal_prd")

    def test_governed_excludes_quick_draft(self):
        intent = classify_channel_intent("Create a quick rough draft PRD", {}, config=self.config)
        self.assertEqual(intent.kind, "informal_prd")

    def test_pending_workflow_is_gate(self):
        intent = classify_channel_intent("yes", {"pending_workflow": {"workflow_type": "prd_request"}})
        self.assertEqual(intent.kind, "workflow_gate")

    def test_new_governed_request_replaces_stale_pending_proposal(self):
        intent = classify_channel_intent(
            "Create a PRD for an online multiplayer checker game",
            {"pending_workflow": {"workflow_type": "prd_request", "summary": "old topic"}},
            config=self.config,
        )
        self.assertEqual(intent.kind, "governed_prd")
        self.assertEqual(intent.workflow_type, "prd_request")

    def test_approved_prd_continue_delivery(self):
        intent = classify_channel_intent(
            "continue delivery",
            {
                "mode": "workflow_linked",
                "workflow_type": "prd_request",
                "workflow_phase": "approved",
                "workflow_id": "wf-1",
            },
            config=self.config,
        )
        self.assertEqual(intent.kind, "continue_bridge")

    def test_start_work_orders_from_prd_routing(self):
        intent = classify_channel_intent(
            "plan work orders from the approved PRD",
            {},
            config=self.config,
        )
        self.assertEqual(intent.kind, "start_work_orders_from_prd")

    def test_approved_prd_work_orders_signal(self):
        intent = classify_channel_intent(
            "generate work orders",
            {
                "mode": "workflow_linked",
                "workflow_type": "prd_request",
                "workflow_phase": "approved",
                "workflow_id": "wf-1",
            },
            config=self.config,
        )
        self.assertEqual(intent.kind, "continue_bridge")

    def test_content_to_jira_signals_are_distinct_from_governed_prd(self):
        cases = [
            "plan work from this content",
            "Please turn this content to work orders",
            "file work orders as jira for the pasted spec",
        ]
        for text in cases:
            intent = classify_channel_intent(text, {}, config=self.config)
            self.assertEqual(intent.kind, "content_to_jira", msg=text)
            self.assertEqual(intent.program_type, "content_to_jira_work_orders")
            self.assertEqual(intent.entry_phase, "intake")
            self.assertIsNone(intent.workflow_type)

        prd = classify_channel_intent("Create a PRD from these requirements", {}, config=self.config)
        self.assertEqual(prd.kind, "governed_prd")
        self.assertNotEqual(prd.kind, "content_to_jira")

    def test_jira_to_pr_signals_are_distinct_from_dev_work(self):
        cases = [
            "show my tickets",
            "pick up my jira for this sprint",
            "implement PROJ-184",
            "please implement pay2-42 today",
        ]
        for text in cases:
            intent = classify_channel_intent(text, {}, config=self.config)
            self.assertEqual(intent.kind, "jira_to_pr", msg=text)
            self.assertEqual(intent.program_type, "jira_to_pr")
            self.assertEqual(intent.entry_phase, "pick")

        for text in ("implement retries in checkout", "implement step-by-step logging"):
            intent = classify_channel_intent(text, {}, config=self.config)
            self.assertNotEqual(intent.kind, "jira_to_pr", msg=text)

        defect = classify_channel_intent("fix bug in checkout retry", {}, config=self.config)
        self.assertEqual(defect.kind, "bug_to_jira")
        self.assertNotEqual(defect.kind, "jira_to_pr")

    def test_bug_to_jira_signals_stay_off_content_and_implement(self):
        cases = [
            "file this bug",
            "please investigate this error from checkout",
            "bug report: totals are wrong",
            "production incident on payments",
        ]
        for text in cases:
            intent = classify_channel_intent(text, {}, config=self.config)
            self.assertEqual(intent.kind, "bug_to_jira", msg=text)
            self.assertEqual(intent.program_type, "bug_to_jira_work_orders")
            self.assertEqual(intent.entry_phase, "bug_intake")

        content = classify_channel_intent("plan work from this content", {}, config=self.config)
        self.assertEqual(content.kind, "content_to_jira")
        for text in ("fix bug in checkout retry", "regression in checkout", "checkout is broken"):
            defect = classify_channel_intent(text, {}, config=self.config)
            self.assertEqual(defect.kind, "bug_to_jira", msg=text)
            self.assertEqual(defect.program_type, "bug_to_jira_work_orders")

    def test_vague_code_asks_before_a_program(self):
        for text in ("open pr", "refactor checkout", "change the checkout"):
            intent = classify_channel_intent(text, {}, config=self.config)
            self.assertEqual(intent.kind, "code_intake", msg=text)
            self.assertIsNone(intent.program_type)
            self.assertIsNone(intent.delegate_agent)

        question = classify_channel_intent("what error did we see", {}, config=self.config)
        self.assertNotEqual(question.kind, "bug_to_jira")

    def test_direct_respond_for_status_questions(self):
        intent = classify_channel_intent("what agents do we have?", {}, config=self.config)
        self.assertEqual(intent.kind, "direct_respond")


if __name__ == "__main__":
    unittest.main()
