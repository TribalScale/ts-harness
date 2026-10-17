"""Implement / prepare intake enrichment for governed work orders."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.delivery_program import DeliveryProgramService
from ts_harness.workflows.expressions import resolve_input_map
from ts_harness.workflows.hooks import hydrate_input
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


class ImplementIntakeTests(unittest.TestCase):
    def test_implement_input_map_includes_body_and_repo_fields(self):
        skeleton = load_skeleton("deliver_work_order")
        stage = skeleton.stage("deliver_work_order")
        self.assertIn("work_order_body", stage.input_map)
        self.assertIn("completed_work_order_ids", stage.input_map)
        self.assertIn("repo_url", stage.input_map)
        self.assertEqual(stage.input_map.get("refinery_artifacts"), "$workflow.refinery_artifacts")
        self.assertEqual(stage.input_map.get("foundry_artifacts"), "$workflow.foundry_artifacts")
        self.assertEqual(stage.input_map.get("planner_artifacts"), "$workflow.planner_artifacts")

    def test_resolve_planning_packs_from_extras(self):
        packs = {
            "refinery_artifacts": {"frds_markdown": "## FRD-001"},
            "foundry_artifacts": {"blueprints_markdown": "## COMP-001"},
            "planner_artifacts": {"work_orders_markdown": "## WO-001"},
        }
        resolved = resolve_input_map(
            {
                "refinery_artifacts": "$workflow.refinery_artifacts",
                "foundry_artifacts": "$workflow.foundry_artifacts",
                "planner_artifacts": "$workflow.planner_artifacts",
            },
            tasks=[],
            extras=packs,
        )
        self.assertEqual(resolved, packs)

    def test_prepare_input_map_includes_completed_ids(self):
        skeleton = load_skeleton("work_orders_from_prd")
        stage = skeleton.stage("prepare_work_order_delivery")
        self.assertEqual(
            stage.input_map.get("completed_work_order_ids"),
            "$workflow.completed_work_order_ids",
        )

    def test_resolve_completed_ids_from_extras(self):
        resolved = resolve_input_map(
            {"completed_work_order_ids": "$workflow.completed_work_order_ids"},
            tasks=[],
            extras={"completed_work_order_ids": ["WO-001"]},
        )
        self.assertEqual(resolved["completed_work_order_ids"], ["WO-001"])

    def test_materialize_implement_seeds_work_order_body(self):
        client = MagicMock()
        client.create_workflow.return_value = {"id": "wf-1"}
        client.link_workflow_to_program.return_value = {}
        client.create_task.return_value = {"id": "task-1"}

        service = DeliveryProgramService(client=client)
        markdown = "# WO-001 Lobby\n\nDo lobby.\n\n# WO-002 Next\n\nNext.\n"
        with patch(
            "ts_harness.implementation.config.repo_for_workflow",
            return_value=MagicMock(url="https://github.com/org/app.git", base_branch="main", clone_depth=1),
        ):
            service._materialize_phase_workflow(
                workflow_type="deliver_work_order",
                program_id="prog-1",
                program_phase="implement",
                tenant_id="default",
                binding_key="slack:C:T",
                requester_actor_id="U1",
                summary="WO-001",
                artifacts={
                    "next_work_order_id": "WO-001",
                    "work_orders_handoff": True,
                    "planning_track": "work_orders",
                    "work_orders_markdown": markdown,
                    "completed_work_order_ids": [],
                    "refinery_artifacts": {"frds_markdown": "## FRD-001\n"},
                    "foundry_artifacts": {"blueprints_markdown": "## COMP-001\n"},
                    "planner_artifacts": {"work_orders_markdown": markdown},
                },
            )

        create_calls = client.create_task.call_args_list
        self.assertTrue(create_calls)
        intake = create_calls[0].args[0]["input"]
        self.assertEqual(intake["work_order_id"], "WO-001")
        self.assertIn("WO-001 Lobby", intake["work_order_body"])
        self.assertNotIn("WO-002", intake["work_order_body"])
        self.assertEqual(intake["repo_url"], "https://github.com/org/app.git")
        self.assertEqual(intake["base_branch"], "main")
        self.assertEqual(intake["refinery_artifacts"]["frds_markdown"], "## FRD-001\n")
        self.assertIn("blueprints_markdown", intake["foundry_artifacts"])
        self.assertEqual(intake["planner_artifacts"]["work_orders_markdown"], markdown)

    def test_on_workflow_terminal_patches_work_orders_markdown(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-plan",
                "workflow_type": "work_orders_from_prd",
                "program_id": "prog-1",
                "program_phase": "work_orders",
            },
            "tasks": [
                {
                    "status": "succeeded",
                    "output": {
                        "planner_complete": True,
                        "work_order_ids": ["WO-001"],
                        "next_work_order_id": "WO-001",
                        "work_orders_handoff": True,
                        "artifacts": {"work_orders_markdown": "# WO-001\n\nBody"},
                    },
                }
            ],
        }
        client.get_program.return_value = {"program": {"metadata": {"artifacts": {}}}}
        service = DeliveryProgramService(client=client)
        result = service.on_workflow_terminal("wf-plan", actor="test")
        self.assertIn("work_orders_markdown", result["artifacts_patched"])
        patch = client.patch_program_artifacts.call_args.args[1]
        self.assertEqual(patch["work_orders_markdown"], "# WO-001\n\nBody")

    def test_on_workflow_terminal_harvests_stage_artifact_packs(self):
        client = MagicMock()
        refinery = {
            "frds_markdown": "## FRD-001\n\nBody",
            "requirements_markdown": "# Req",
            "feature_tree_markdown": "# Tree",
            "product_overview_markdown": "# Overview",
        }
        foundry = {
            "blueprints_markdown": "## COMP-001\n\nServer",
            "architecture_decisions_markdown": "## ADR-001\n\nDecide",
            "architecture_summary_markdown": "# Summary",
        }
        planner = {
            "work_orders_markdown": "## WO-001\n\nBody",
            "context_graph_markdown": "# Graph",
            "execution_phases_markdown": "# Phases",
        }
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-plan",
                "workflow_type": "work_orders_from_prd",
                "program_id": "prog-1",
                "program_phase": "work_orders",
            },
            "tasks": [
                {
                    "step_name": "refine_requirements",
                    "status": "succeeded",
                    "output": {"artifacts": refinery},
                },
                {
                    "step_name": "design_blueprints",
                    "status": "succeeded",
                    "output": {"artifacts": foundry},
                },
                {
                    "step_name": "plan_work_orders",
                    "status": "succeeded",
                    "output": {
                        "planner_complete": True,
                        "work_order_ids": ["WO-001"],
                        "next_work_order_id": "WO-001",
                        "artifacts": planner,
                    },
                },
            ],
        }
        client.get_program.return_value = {"program": {"metadata": {"artifacts": {}}}}
        service = DeliveryProgramService(client=client)
        result = service.on_workflow_terminal("wf-plan", actor="test")
        patch = client.patch_program_artifacts.call_args.args[1]
        self.assertEqual(patch["refinery_artifacts"], refinery)
        self.assertEqual(patch["foundry_artifacts"], foundry)
        self.assertEqual(patch["planner_artifacts"], planner)
        self.assertEqual(patch["work_orders_markdown"], "## WO-001\n\nBody")
        self.assertIn("refinery_artifacts", result["artifacts_patched"])
        self.assertIn("foundry_artifacts", result["artifacts_patched"])
        self.assertIn("planner_artifacts", result["artifacts_patched"])

    def test_hydrate_loads_program_completed_ids(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf", "program_id": "prog-1"},
            "tasks": [],
        }
        client.get_program.return_value = {
            "program": {
                "metadata": {
                    "artifacts": {"completed_work_order_ids": ["WO-001"]},
                }
            }
        }
        client.patch_task_input.return_value = {"ok": True}
        skeleton = load_skeleton("work_orders_from_prd")
        stage = skeleton.stage("prepare_work_order_delivery")
        ctx = HookContext(
            workflow_id="wf",
            workflow_type="work_orders_from_prd",
            tenant_id="default",
            task={"id": "t1", "input": {}},
            stage=stage,
            skeleton=skeleton,
            actor="test",
            memory_client=client,
            extras={},
        )
        hydrate_input.handle(ctx)
        merged = client.patch_task_input.call_args.args[1]
        self.assertEqual(merged["completed_work_order_ids"], ["WO-001"])

    def test_hydrate_loads_planning_packs_for_implement(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-impl", "program_id": "prog-1"},
            "tasks": [],
        }
        packs = {
            "refinery_artifacts": {"frds_markdown": "## FRD-001"},
            "foundry_artifacts": {"blueprints_markdown": "## COMP-001"},
            "planner_artifacts": {"work_orders_markdown": "## WO-001"},
        }
        client.get_program.return_value = {
            "program": {"metadata": {"artifacts": packs}},
        }
        client.patch_task_input.return_value = {"ok": True}
        skeleton = load_skeleton("deliver_work_order")
        stage = skeleton.stage("deliver_work_order")
        ctx = HookContext(
            workflow_id="wf-impl",
            workflow_type="deliver_work_order",
            tenant_id="default",
            task={"id": "t1", "input": {}},
            stage=stage,
            skeleton=skeleton,
            actor="test",
            memory_client=client,
            extras={},
        )
        hydrate_input.handle(ctx)
        merged = client.patch_task_input.call_args.args[1]
        self.assertEqual(merged["refinery_artifacts"], packs["refinery_artifacts"])
        self.assertEqual(merged["foundry_artifacts"], packs["foundry_artifacts"])
        self.assertEqual(merged["planner_artifacts"], packs["planner_artifacts"])


if __name__ == "__main__":
    unittest.main()
