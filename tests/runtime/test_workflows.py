"""Tests for governed delivery workflow promotion and revision."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.gateway.delegate_confirmation import (
    resolve_workflow_confirmation,
    set_pending_workflow,
)
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy
from ts_harness.config.schema import load_runtime_config
from ts_harness.workflows.materialize import load_skeleton, materialize_skeleton_tasks
from ts_harness.workflows.promotion import WorkflowPromotionService


class WorkflowMaterializeTests(unittest.TestCase):
    def test_governed_delivery_legacy_stub_loads(self):
        skeleton = load_skeleton("governed_delivery")
        self.assertEqual(skeleton.workflow_type, "governed_delivery")
        self.assertTrue(skeleton.deprecated)
        self.assertEqual(skeleton.stages[0].name, "create_jira")

    def test_jira_from_prd_skeleton_loads(self):
        skeleton = load_skeleton("jira_from_prd")
        self.assertEqual(skeleton.workflow_type, "jira_from_prd")
        self.assertEqual(skeleton.stages[0].name, "create_jira")

    def test_work_orders_from_prd_skeleton_loads(self):
        skeleton = load_skeleton("work_orders_from_prd")
        self.assertEqual(skeleton.workflow_type, "work_orders_from_prd")
        self.assertEqual(skeleton.stages[0].name, "refine_requirements")

    def test_implement_work_orders_skeleton_loads(self):
        skeleton = load_skeleton("implement_work_orders")
        self.assertEqual(skeleton.stages[0].name, "deliver_wave")
        self.assertEqual(load_skeleton("deliver_work_order").stages[0].name, "deliver_work_order")

    def test_prd_request_skeleton_loads(self):
        skeleton = load_skeleton("prd_request")
        self.assertEqual(skeleton.stages[0].name, "intake")
        self.assertTrue(any(stage.name == "approve_prd" for stage in skeleton.stages))

    def test_rfp_request_skeleton_loads(self):
        skeleton = load_skeleton("rfp_request")
        self.assertEqual(skeleton.workflow_type, "rfp_request")
        self.assertTrue(any(stage.name == "approve_rfp" for stage in skeleton.stages))
        self.assertIsNotNone(skeleton.session)

    def test_materialize_includes_dependencies(self):
        skeleton = load_skeleton("prd_request")
        tasks = materialize_skeleton_tasks(skeleton, workflow_id="wf-1")
        approve = next(task for task in tasks if task["step_name"] == "approve_prd")
        self.assertEqual(approve["depends_on"], ["draft_prd"])


class WorkflowConfirmationTests(unittest.TestCase):
    def test_workflow_confirm_on_proceed(self):
        session = SessionRecord(
            session_id="s1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
        )
        set_pending_workflow(session, workflow_type="jira_from_prd", summary="checkers PRD")
        result = resolve_workflow_confirmation(session, "proceed")
        self.assertTrue(result.confirmed)


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_runtime_config("config/ts-harness-runtime.test.yaml")
        self.agent = self.config.get_agent("orchestrator")

    def test_start_workflow_denied_without_confirmation(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            workflow_confirmed=False,
        )
        decision = evaluate_tool_policy("start_workflow", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")

    def test_workflow_approve_denied_without_linked_session(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            workflow_linked=False,
        )
        decision = evaluate_tool_policy("workflow_approve", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")

    def test_workflow_approve_allowed_when_linked(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            workflow_linked=True,
            linked_workflow_id="wf-1",
        )
        decision = evaluate_tool_policy("workflow_approve", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "allow")

    def test_auto_delegate_allowed_for_pm_agent_pending(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            pending_delegation={"agent_id": "pm-agent", "task": "draft a PRD outline"},
        )
        decision = evaluate_tool_policy("delegate_task", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "allow")


class WorkflowPromotionTests(unittest.TestCase):
    def test_promotion_links_session_metadata(self):
        client = MagicMock()
        client.create_workflow.return_value = {"id": "wf-1"}
        client.create_task.side_effect = [
            {"id": "t1"},
            {"id": "t2"},
            {"id": "t3"},
            {"id": "t4"},
        ]
        session = SessionRecord(
            session_id="s-promote",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"channel": "slack", "channel_id": "C1", "thread_id": "123.456"},
        )
        get_session_store().save(session)
        service = WorkflowPromotionService(client=client)
        result = service.start(
            session=session,
            workflow_type="prd_request",
            summary="checkers game PRD",
            tenant_id="default",
            binding_key="slack:C1:123.456",
        )
        self.assertTrue(result.workflow_id)
        client.create_program.assert_called_once()
        client.create_workflow.assert_called_once()
        program_call = client.create_program.call_args.kwargs
        workflow_call = client.create_workflow.call_args.kwargs
        self.assertEqual(
            program_call["program_id"],
            workflow_call["program_id"],
        )
        client.get_workflow.assert_not_called()
        self.assertEqual(len(client.create_task.call_args_list), 4)
        saved = get_session_store().get("s-promote")
        self.assertEqual(saved.metadata.get("mode"), "workflow_linked")
        self.assertEqual(saved.metadata.get("workflow_type"), "prd_request")
        self.assertTrue(saved.metadata.get("program_id"))


class WorkflowRevisionTests(unittest.TestCase):
    @patch("ts_harness.workflows.engine.get_memory_client")
    def test_engine_reject_creates_revision_tasks(self, mock_client_factory):
        client = MagicMock()
        mock_client_factory.return_value = client
        client.get_workflow.return_value = {
            "workflow": {"workflow_type": "prd_request"},
            "tasks": [
                {
                    "id": "draft-1",
                    "step_name": "draft_prd",
                    "status": "succeeded",
                    "output": {"summary": "draft"},
                    "created_at": "2026-01-01",
                },
                {
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "approval_status": "pending",
                    "status": "pending",
                    "workflow_type": "prd_request",
                },
            ],
        }

        with patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk") as mock_bulk:
            mock_bulk.side_effect = [["rev-1"], ["gate-2"]]
            from ts_harness.workflows.engine import WorkflowEngine

            engine = WorkflowEngine(client=client)
            result = engine.handle_reject(
                workflow_id="wf-1",
                gate_task={
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "workflow_type": "prd_request",
                    "approval_status": "pending",
                },
                comments="add mobile section",
                actor="orchestrator",
            )
        self.assertEqual(result["revise_task_id"], "rev-1")
        client.reject_task.assert_called_once_with("gate-1", actor="orchestrator")


if __name__ == "__main__":
    unittest.main()
