"""Contract tests for work-orders delivery smoke (CI, no live Slack/GitHub)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from ts_harness.workflows.delivery_program import DeliveryProgramService
from ts_harness.workflows.hooks import complete_program_phase
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "work-orders-smoke"


class WorkOrdersDeliverySmokeContractTests(unittest.TestCase):
    def test_fixture_prd_exists(self):
        prd = FIXTURES / "prd-checkers.md"
        self.assertTrue(prd.is_file())
        self.assertIn("checkers", prd.read_text(encoding="utf-8").lower())

    def test_manifest_stub_valid_json(self):
        manifest = json.loads((FIXTURES / "manifest-stub.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["planner"]["status"], "complete")

    def test_program_advance_work_orders_handoff(self):
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-smoke",
                "current_phase": "work_orders",
                "summary": "Checkers",
                "metadata": {
                    "artifacts": {
                        "approved_prd": {"path": str(FIXTURES / "prd-checkers.md")},
                        "planner_complete": True,
                        "work_order_ids": ["WO-001"],
                        "next_work_order_id": "WO-001",
                        "work_orders_handoff": True,
                        "planning_track": "work_orders",
                    },
                },
            },
            "workflows": [],
        }
        client.advance_program.return_value = {"current_phase": "implement"}
        client.create_workflow.return_value = {"id": "wf-impl-wo"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-smoke"}

        service = DeliveryProgramService(client=client)
        result = service.advance_program(
            program_id="prog-smoke",
            target_phase="implement",
            summary="Deliver WO-001",
        )
        self.assertEqual(result["workflow_type"], "implement_work_orders")
        self.assertEqual(result["first_step"], "deliver_wave")

    def test_complete_program_phase_reenters_implement_for_next_wo(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-impl-wo",
                "workflow_type": "implement_work_orders",
                "program_id": "prog-smoke",
                "program_phase": "implement",
                "summary": "WO-001",
            },
            "tasks": [
                {"status": "succeeded", "output": {"work_order_id": "WO-001", "pr_url": "https://example/pr/1"}},
                {"status": "succeeded"},
            ],
        }
        client.get_program.return_value = {
            "program": {
                "current_phase": "implement",
                "metadata": {
                    "artifacts": {
                        "work_order_ids": ["WO-001", "WO-002"],
                        "completed_work_order_ids": ["WO-001"],
                        "planning_track": "work_orders",
                    },
                },
            },
        }
        client.advance_program.return_value = {"current_phase": "implement"}
        client.create_workflow.return_value = {"id": "wf-impl-wo-2"}
        client.create_task.return_value = {"id": "task-2"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-smoke"}
        client.patch_program_artifacts.return_value = {}

        service = DeliveryProgramService(client=client)
        skeleton = load_skeleton("implement_work_orders")
        ctx = HookContext(
            workflow_id="wf-impl-wo",
            workflow_type="implement_work_orders",
            tenant_id="default",
            task={"id": "t-final"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )
        with unittest.mock.patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
            return_value=service,
        ):
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "phase_reentered_implement")
        self.assertEqual(result["advance"]["workflow_type"], "implement_work_orders")
        client.complete_workflow.assert_called_once()
        kwargs = client.complete_workflow.call_args.kwargs
        self.assertEqual(kwargs.get("reason"), "phase_complete")
        self.assertEqual(kwargs.get("superseded_by"), result["advance"]["workflow_id"])
        # Re-entry must seed WO-002 on the new implement intake task.
        create_task_inputs = [
            call.args[0].get("input") or {}
            for call in client.create_task.call_args_list
            if call.args
        ]
        self.assertTrue(
            any(inp.get("work_order_id") == "WO-002" for inp in create_task_inputs),
            create_task_inputs,
        )


if __name__ == "__main__":
    unittest.main()
