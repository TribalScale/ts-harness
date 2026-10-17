from __future__ import annotations

import unittest

from ts_harness.workflows.program_adapters import build_default_adapter_registry


class DeclaredOutputsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_default_adapter_registry()

    def test_extract_declared_outputs_copies_produces_keys(self):
        patch = self.registry.extract_outputs(
            "extract_declared_outputs",
            workflow_type="rfp_review",
            tasks=[
                {
                    "status": "succeeded",
                    "output": {
                        "rfp_markdown": "# RFP",
                        "noise": "ignore-me",
                        "approved_artifact": {"body": "approved"},
                    },
                }
            ],
            produces_artifacts=["approved_rfp", "rfp_markdown"],
        )
        self.assertEqual(patch["rfp_markdown"], "# RFP")
        self.assertEqual(patch["approved_rfp"], {"body": "approved"})
        self.assertNotIn("noise", patch)

    def test_build_declared_inputs_copies_required_artifacts(self):
        intake = self.registry.build_inputs(
            "extract_declared_outputs",
            summary="Draft RFP",
            artifacts={"approved_rfp": {"title": "x"}, "other": 1},
            required_artifacts=["approved_rfp"],
        )
        self.assertEqual(intake["summary"], "Draft RFP")
        self.assertEqual(intake["approved_rfp"], {"title": "x"})
        self.assertNotIn("other", intake)

    def test_extract_implement_tracks_cloned_workflow_type(self):
        patch = self.registry.extract_outputs(
            "extract_implement_outputs",
            workflow_type="implement_work_orders_ui",
            tasks=[
                {
                    "status": "succeeded",
                    "step_name": "deliver_work_order",
                    "output": {
                        "work_order_id": "WO-002",
                        "pr_url": "https://example.com/pr/2",
                    },
                }
            ],
            artifacts={"completed_work_order_ids": ["WO-001"], "planning_track": "work_orders"},
        )
        self.assertEqual(patch["completed_work_order_ids"], ["WO-001", "WO-002"])
        self.assertEqual(patch["pr_url"], "https://example.com/pr/2")

    def test_extract_work_orders_tracks_cloned_workflow_type(self):
        patch = self.registry.extract_outputs(
            "extract_work_orders_outputs",
            workflow_type="work_orders_from_prd_ui",
            tasks=[
                {
                    "status": "succeeded",
                    "step_name": "refine_requirements",
                    "output": {"artifacts": {"notes": "ok"}},
                },
                {
                    "status": "succeeded",
                    "step_name": "plan_work_orders",
                    "output": {
                        "planner_complete": True,
                        "next_work_order_id": "WO-001",
                        "work_order_ids": ["WO-001"],
                        "work_orders_handoff": True,
                        "artifacts": {"plan": "a"},
                    },
                },
            ],
        )
        self.assertEqual(patch["planning_track"], "work_orders")
        self.assertEqual(patch["refinery_artifacts"], {"notes": "ok"})
        self.assertEqual(patch["planner_artifacts"], {"plan": "a"})
        self.assertEqual(patch["next_work_order_id"], "WO-001")

    def test_implement_jira_ticket_prefers_default_branch_over_generic_main(self):
        intake = self.registry.build_inputs(
            "extract_implement_outputs",
            summary="Implement TFS-184",
            artifacts={
                "jira_issue_key": "TFS-184",
                "default_branch": "dev",
            },
            extra_input={"base_branch": "main"},
            workflow_type="implement_jira_ticket",
        )
        self.assertEqual(intake["jira_issue_key"], "TFS-184")
        self.assertEqual(intake["base_branch"], "dev")

    def test_implement_jira_ticket_rewrites_bare_main_to_dev(self):
        intake = self.registry.build_inputs(
            "extract_implement_outputs",
            summary="Implement TFS-184",
            artifacts={"jira_issue_key": "TFS-184"},
            extra_input={"base_branch": "main"},
            workflow_type="implement_jira_ticket",
        )
        self.assertEqual(intake["base_branch"], "dev")


if __name__ == "__main__":
    unittest.main()
