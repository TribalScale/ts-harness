"""Program phase completion must run after task success is persisted."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.engine import WorkflowEngine
from ts_harness.workflows.hooks import build_default_registry, complete_program_phase
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


class ProgramPhaseAfterPersistTests(unittest.TestCase):
    def test_on_task_transition_skips_phase_complete_when_not_persisted(self):
        client = MagicMock()
        registry = build_default_registry()
        engine = WorkflowEngine(client=client, registry=registry)
        with patch.object(engine, "_maybe_complete_program_phase") as complete:
            engine.on_task_transition(
                {
                    "id": "t-prep",
                    "workflow_id": "wf-wo",
                    "workflow_type": "work_orders_from_prd",
                    "tenant_id": "default",
                    "step_name": "prepare_work_order_delivery",
                    "status": "claimed",
                    "agent_id": "orchestrator",
                },
                "succeeded",
                dispatch_result={
                    "output": {
                        "planner_complete": True,
                        "work_orders_handoff": True,
                        "next_work_order_id": "WO-001",
                    }
                },
            )
        complete.assert_not_called()

    def test_after_task_succeeded_invokes_phase_complete(self):
        client = MagicMock()
        registry = build_default_registry()
        engine = WorkflowEngine(client=client, registry=registry)
        with patch.object(
            engine,
            "_maybe_complete_program_phase",
            return_value={"status": "phase_completed_auto_advanced"},
        ) as complete:
            result = engine.after_task_succeeded(
                {
                    "id": "t-prep",
                    "workflow_id": "wf-wo",
                    "workflow_type": "work_orders_from_prd",
                    "tenant_id": "default",
                    "step_name": "prepare_work_order_delivery",
                    "agent_id": "orchestrator",
                },
                dispatch_result={
                    "output": {
                        "planner_complete": True,
                        "work_orders_handoff": True,
                        "next_work_order_id": "WO-001",
                    }
                },
            )
        complete.assert_called_once()
        self.assertEqual(result["status"], "phase_completed_auto_advanced")
        ctx = complete.call_args.args[0]
        self.assertEqual(ctx.task.get("status"), "succeeded")

    def test_after_task_succeeded_rehydrates_downstream_after_persist(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-impl", "program_id": None},
            "tasks": [
                {
                    "id": "deliver-1",
                    "step_name": "deliver_work_order",
                    "status": "succeeded",
                    "output": {
                        "pr_url": "https://github.com/org/app/pull/7",
                        "work_order_id": "WO-002",
                    },
                },
                {
                    "id": "qa-1",
                    "step_name": "qa_gate",
                    "status": "pending",
                    "input": {"pr_url": None},
                },
            ],
        }
        client.patch_task_input.side_effect = lambda task_id, merged, actor=None: {
            "id": task_id,
            "input": merged,
        }
        registry = build_default_registry()
        engine = WorkflowEngine(client=client, registry=registry)
        with patch.object(engine, "_maybe_complete_program_phase", return_value=None):
            engine.after_task_succeeded(
                {
                    "id": "deliver-1",
                    "workflow_id": "wf-impl",
                    "workflow_type": "deliver_work_order",
                    "tenant_id": "default",
                    "step_name": "deliver_work_order",
                    "status": "succeeded",
                    "output": {
                        "pr_url": "https://github.com/org/app/pull/7",
                        "work_order_id": "WO-002",
                    },
                },
                dispatch_result={
                    "output": {
                        "pr_url": "https://github.com/org/app/pull/7",
                        "work_order_id": "WO-002",
                    }
                },
            )
        patched = client.patch_task_input.call_args.args[1]
        self.assertEqual(patched["pr_url"], "https://github.com/org/app/pull/7")
        self.assertEqual(patched["work_order_id"], "WO-002")

    def test_auto_advance_marks_source_workflow_complete(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-wo",
                "workflow_type": "work_orders_from_prd",
                "program_id": "prog-1",
                "program_phase": "work_orders",
                "source_channel": "slack:D1:1.2",
                "summary": "plan work orders",
            },
            "tasks": [
                {
                    "status": "succeeded",
                    "output": {
                        "planner_complete": True,
                        "work_orders_handoff": True,
                        "next_work_order_id": "WO-001",
                        "work_order_ids": ["WO-001"],
                    },
                }
            ],
        }
        service = MagicMock()
        service.on_workflow_terminal.return_value = {
            "program_id": "prog-1",
            "artifacts_patched": ["work_orders_handoff", "next_work_order_id"],
        }
        service.advance_program.return_value = {
            "workflow_id": "wf-impl",
            "workflow_type": "deliver_work_order",
            "target_phase": "implement",
        }
        skeleton = load_skeleton("work_orders_from_prd")
        ctx = HookContext(
            workflow_id="wf-wo",
            workflow_type="work_orders_from_prd",
            tenant_id="default",
            task={"id": "t-prep", "status": "succeeded"},
            stage=skeleton.stage("prepare_work_order_delivery"),
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )
        with patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
            return_value=service,
        ):
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "phase_completed_auto_advanced")
        client.complete_workflow.assert_called_once()
        kwargs = client.complete_workflow.call_args.kwargs
        self.assertEqual(kwargs.get("superseded_by"), "wf-impl")


if __name__ == "__main__":
    unittest.main()
