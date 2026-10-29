"""Tests for continue_delivery bridge hook."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.workflows.hooks import continue_delivery
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


class ContinueDeliveryHookTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()

    def test_creates_jira_from_prd_with_seeded_input(self):
        session = SessionRecord(
            session_id="s-bridge",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "workflow_id": "wf-prd",
                "source_workflow_id": "wf-prd",
                "mode": "workflow_linked",
                "workflow_type": "prd_request",
                "workflow_phase": "approved",
                "binding_key": "slack:C1:123.456",
                "approved_artifact": {"summary": "approved PRD"},
            },
        )
        get_session_store().save(session)

        client = MagicMock()
        client.create_workflow.return_value = {"id": "wf-jira"}
        client.create_task.side_effect = [{"id": f"t{index}"} for index in range(1, 5)]

        skeleton = load_skeleton("prd_request")
        ctx = HookContext(
            workflow_id="wf-prd",
            workflow_type="prd_request",
            tenant_id="default",
            task={"id": "gate-1", "step_name": "approve_prd"},
            stage=skeleton.stage("approve_prd"),
            skeleton=skeleton,
            actor="orchestrator",
            memory_client=client,
            extras={
                "session": session,
                "source_workflow_id": "wf-prd",
                "approved_artifact": {"summary": "approved PRD"},
                "binding_key": "slack:C1:123.456",
                "summary": "Continue to Jira and delivery",
            },
        )

        result = continue_delivery.handle(ctx)
        self.assertEqual(result["status"], "bridge_started")
        self.assertEqual(result["first_step"], "review_prd")
        self.assertEqual(len(result["task_ids"]), 4)

        client.complete_workflow.assert_called_once()
        complete_kwargs = client.complete_workflow.call_args.kwargs
        self.assertNotIn("superseded_by", complete_kwargs)

        create_payload = client.create_workflow.call_args.kwargs
        self.assertEqual(create_payload["workflow_type"], "jira_from_prd")
        self.assertEqual(create_payload["parent_workflow_id"], "wf-prd")

        first_task_payload = client.create_task.call_args_list[0].args[0]
        self.assertEqual(first_task_payload["step_name"], "review_prd")
        self.assertEqual(first_task_payload["input"]["approved_prd"], {"summary": "approved PRD"})

        saved = get_session_store().get("s-bridge")
        self.assertEqual(saved.metadata.get("mode"), "bridge_linked")
        self.assertEqual(saved.metadata.get("workflow_type"), "jira_from_prd")


if __name__ == "__main__":
    unittest.main()
