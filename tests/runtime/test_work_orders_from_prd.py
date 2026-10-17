"""Tests for work_orders_from_prd skeleton."""

from __future__ import annotations

import unittest

from ts_harness.workflows.materialize import load_skeleton, materialize_skeleton_tasks


class WorkOrdersFromPrdSkeletonTests(unittest.TestCase):
    def test_skeleton_loads(self):
        skeleton = load_skeleton("work_orders_from_prd")
        self.assertEqual(skeleton.workflow_type, "work_orders_from_prd")
        self.assertEqual(skeleton.program.get("phase"), "work_orders")
        self.assertEqual(skeleton.intake_stage_name(), "refine_requirements")

    def test_stages_include_prepare_step(self):
        skeleton = load_skeleton("work_orders_from_prd")
        names = [stage.name for stage in skeleton.stages]
        self.assertIn("prepare_work_order_delivery", names)
        self.assertIn("approve_work_orders", names)

    def test_materialize_seeds_approved_prd(self):
        skeleton = load_skeleton("work_orders_from_prd")
        tasks = materialize_skeleton_tasks(
            skeleton,
            workflow_id="wf-wo",
            stage_inputs={
                "refine_requirements": {
                    "approved_prd": {"title": "PRD"},
                },
            },
        )
        refine = next(task for task in tasks if task["step_name"] == "refine_requirements")
        self.assertEqual(refine["input"]["approved_prd"], {"title": "PRD"})
        self.assertEqual(refine["input"]["stage"], "refinery")

    def test_materialize_seeds_literal_input_map_stage_for_each_specialist_step(self):
        skeleton = load_skeleton("work_orders_from_prd")
        tasks = materialize_skeleton_tasks(skeleton, workflow_id="wf-wo")
        by_name = {task["step_name"]: task for task in tasks}
        self.assertEqual(by_name["design_blueprints"]["input"]["stage"], "foundry")
        self.assertEqual(by_name["plan_work_orders"]["input"]["stage"], "planner")

    def test_specialist_stages_present_for_approval(self):
        skeleton = load_skeleton("work_orders_from_prd")
        for stage_name in (
            "refine_requirements",
            "design_blueprints",
            "plan_work_orders",
        ):
            with self.subTest(stage_name=stage_name):
                self.assertEqual(
                    skeleton.stage(stage_name).present_prompt,
                    "approval",
                )

    def test_approval_gates_hydrate_next_stage_inputs(self):
        skeleton = load_skeleton("work_orders_from_prd")

        expectations = {
            "approve_refinery": ["design_blueprints"],
            "approve_foundry": ["plan_work_orders"],
            "approve_work_orders": ["prepare_work_order_delivery"],
        }
        for stage_name, downstream in expectations.items():
            with self.subTest(stage_name=stage_name):
                on_complete = skeleton.stage(stage_name).on_complete or {}
                self.assertEqual(on_complete.get("hook"), "hydrate_input")
                self.assertEqual(on_complete.get("downstream"), downstream)

    def test_governed_skill_hints_on_planning_stages(self):
        skeleton = load_skeleton("work_orders_from_prd")
        for stage_name in (
            "refine_requirements",
            "design_blueprints",
            "plan_work_orders",
        ):
            with self.subTest(stage_name=stage_name):
                self.assertEqual(
                    skeleton.stage(stage_name).skill_hints,
                    ["prd-to-work-orders-governed"],
                )

    def test_downstream_input_map_chains_prior_artifacts(self):
        skeleton = load_skeleton("work_orders_from_prd")
        foundry_map = skeleton.stage("design_blueprints").input_map
        planner_map = skeleton.stage("plan_work_orders").input_map
        self.assertEqual(
            foundry_map.get("prior_refinery"),
            "$prior.refine_requirements.output.artifacts",
        )
        self.assertEqual(
            planner_map.get("prior_foundry"),
            "$prior.design_blueprints.output.artifacts",
        )

    def test_prepare_step_uses_governed_source_skill(self):
        skeleton = load_skeleton("work_orders_from_prd")
        prepare = skeleton.stage("prepare_work_order_delivery")
        self.assertEqual(
            prepare.skill_hints,
            ["source-work-order-governed"],
        )
        self.assertEqual(
            prepare.input_map.get("planner_artifacts"),
            "$prior.plan_work_orders.output.artifacts",
        )


if __name__ == "__main__":
    unittest.main()
