"""Phase 2 Jira delivery repair: aggregate barrier, qa_gate, match_all merge."""

from __future__ import annotations

import unittest
from typing import Any

from ts_harness.workflows.hooks import collect_delivery_results as cdr
from ts_harness.workflows.hooks import wait_external
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


PR1 = "https://github.com/org/repo/pull/1"
PR2 = "https://github.com/org/repo/pull/2"


class ImplementDeliveryGraphTests(unittest.TestCase):
    def test_definition_version_bumped(self):
        skeleton = load_skeleton("implement_delivery")
        self.assertEqual(skeleton.definition_version, "1.3.0")
        self.assertTrue(skeleton.program.get("terminal"))
        self.assertFalse(skeleton.program.get("next_phase"))

    def test_graph_inserts_barrier_and_qa_before_merge(self):
        skeleton = load_skeleton("implement_delivery")
        names = [stage.name for stage in skeleton.stages]
        self.assertEqual(
            names,
            [
                "implement_tickets",
                "collect_delivery_results",
                "await_pr_checks",
                "qa_gate",
                "await_human_merge",
            ],
        )
        checks = skeleton.stage("await_pr_checks")
        self.assertEqual(checks.depends_on, ["collect_delivery_results"])
        self.assertEqual(
            checks.wait_external["pr_checks"],
            {"pr_urls": "$prior.collect_delivery_results.output.pr_urls"},
        )

    def test_barrier_hydrates_qa_and_depends_on_expand(self):
        skeleton = load_skeleton("implement_delivery")
        barrier = skeleton.stage("collect_delivery_results")
        self.assertEqual(barrier.barrier.get("hook"), "collect_delivery_results")
        self.assertEqual(barrier.depends_on, ["implement_tickets"])
        self.assertEqual(barrier.on_complete.get("downstream"), ["qa_gate"])

    def test_qa_gate_pulls_full_pr_set(self):
        skeleton = load_skeleton("implement_delivery")
        qa = skeleton.stage("qa_gate")
        self.assertEqual(qa.depends_on, ["await_pr_checks"])
        self.assertEqual(qa.input_map["pr_checks"], "$prior.await_pr_checks.output.prs")
        self.assertEqual(
            qa.input_map["pr_urls"],
            "$prior.collect_delivery_results.output.pr_urls",
        )
        self.assertEqual(qa.present_prompt, "merge")

    def test_dynamic_tasks_use_phase1_allowed_contracts(self):
        skeleton = load_skeleton("implement_delivery")
        expand = skeleton.stage("implement_tickets")
        self.assertEqual(expand.allowed_contracts, ["implement_delivery_task"])

    def test_await_uses_match_all(self):
        skeleton = load_skeleton("implement_delivery")
        merge = skeleton.stage("await_human_merge")
        self.assertEqual(merge.depends_on, ["qa_gate"])
        self.assertEqual(
            merge.wait_external["match_all"]["source"],
            "$prior.qa_gate.output.result.prs",
        )


class BarrierAggregationTests(unittest.TestCase):
    def _tasks(self, child_statuses):
        barrier = {
            "id": "barrier-1",
            "step_name": "collect_delivery_results",
            "depends_on": ["impl-1", "impl-2"],
        }
        children = [
            {
                "id": "impl-1",
                "step_name": "implement_TFS-1",
                "status": child_statuses[0],
                "input": {"work_item_ref": "PROJ-1"},
                "output": {"pr_url": PR2},
            },
            {
                "id": "impl-2",
                "step_name": "implement_TFS-2",
                "status": child_statuses[1],
                "input": {"work_item_ref": "PROJ-2"},
                "output": {"pr_url": PR1},
            },
        ]
        return barrier, [barrier, *children]

    def test_multi_pr_aggregate_dedups_and_sorts(self):
        barrier, tasks = self._tasks(["succeeded", "succeeded"])
        result = cdr.aggregate_delivery_results(barrier, tasks)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"]["pr_urls"], [PR1, PR2])
        refs = {pr["work_item_ref"] for pr in result["output"]["result"]["prs"]}
        self.assertEqual(refs, {"PROJ-1", "PROJ-2"})

    def test_failed_child_blocks_barrier(self):
        barrier, tasks = self._tasks(["succeeded", "failed"])
        result = cdr.aggregate_delivery_results(barrier, tasks)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("failed", result["reason"])

    def test_non_terminal_child_blocks_barrier(self):
        barrier, tasks = self._tasks(["succeeded", "running"])
        result = cdr.aggregate_delivery_results(barrier, tasks)
        self.assertEqual(result["status"], "blocked")

    def test_succeeded_child_without_pr_url_blocks(self):
        barrier = {
            "id": "b",
            "step_name": "collect_delivery_results",
            "depends_on": ["c1"],
        }
        tasks = [
            barrier,
            {"id": "c1", "step_name": "impl", "status": "succeeded", "output": {}},
        ]
        result = cdr.aggregate_delivery_results(barrier, tasks)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("pr_url", result["reason"])


class _LegacyStage:
    """Minimal stage mimicking a v1 single-match await_human_merge wait."""

    approval_required = False
    wait_external = {
        "event_type": "github.merge",
        "match": {"pr_url": "$prior.deliver_work_order.output.pr_url"},
    }


class _LegacySkeleton:
    workflow_type = "implement_work_orders"

    def stage(self, name):
        return _LegacyStage()


class LegacyV1CompatibilityTests(unittest.TestCase):
    """Legacy two-stage implement workflows keep exact single-PR match resume."""

    def test_single_match_resume_still_works(self):
        tasks = [
            {
                "id": "deliver-1",
                "step_name": "deliver_work_order",
                "status": "succeeded",
                "output": {"pr_url": PR1},
                "created_at": "2026-01-01",
            },
            {
                "id": "await-1",
                "step_name": "await_human_merge",
                "status": "blocked",
                "error": "awaiting_external:github.merge",
                "expected_event_type": "github.merge",
                "created_at": "2026-01-02",
            },
        ]
        resumed: list[dict[str, Any]] = []

        class Mem:
            def get_workflow(self, workflow_id):
                return {"workflow": {}, "tasks": tasks}

            def resume_external(self, workflow_id, *, event_type, payload, actor, task_output=None):
                resumed.append({"payload": payload, "task_output": task_output})
                return {"status": "resumed", "task_id": "await-1"}

        ctx = HookContext(
            workflow_id="wf-legacy",
            workflow_type="implement_work_orders",
            tenant_id="default",
            task=None,
            stage=None,
            skeleton=_LegacySkeleton(),
            actor="orchestrator",
            memory_client=Mem(),
            extras={"event_type": "github.merge", "payload": {"pr_url": PR1}},
        )
        result = wait_external.resume(ctx)
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(len(resumed), 1)


if __name__ == "__main__":
    unittest.main()
