"""An approval can be decided only once the step it approves has finished."""

from __future__ import annotations

import unittest
from unittest import mock

from ts_harness.workflows.operability import GateNotReady, build_operability_status, gate_is_ready

# Refinery approved; Foundry has not run. Its gate and the work-order gate are pending too.
TASKS = [
    {"id": "r", "step_name": "refine_requirements", "status": "succeeded", "created_at": "1"},
    {"id": "g1", "step_name": "approve_refinery", "status": "succeeded", "approval_required": True,
     "approval_status": "approved", "depends_on": ["r"], "created_at": "2"},
    {"id": "f", "step_name": "design_blueprints", "status": "running", "depends_on": ["g1"], "created_at": "3"},
    {"id": "g2", "step_name": "approve_foundry", "status": "pending", "approval_required": True,
     "approval_status": "pending", "depends_on": ["f"], "created_at": "4"},
    {"id": "p", "step_name": "plan_work_orders", "status": "pending", "depends_on": ["g2"], "created_at": "5"},
    {"id": "g3", "step_name": "approve_work_orders", "status": "pending", "approval_required": True,
     "approval_status": "pending", "depends_on": ["p"], "created_at": "6"},
]


class GateReadinessTests(unittest.TestCase):
    def test_a_later_gate_is_not_listed_while_its_step_runs(self):
        self.assertFalse(gate_is_ready(TASKS[3], TASKS))
        status = build_operability_status(workflow={"status": "running"}, tasks=TASKS)
        self.assertNotEqual((status.get("waiting_on") or {}).get("kind"), "approval")
        finished = [dict(t, status="succeeded") if t["id"] == "f" else t for t in TASKS]
        status = build_operability_status(workflow={"status": "running"}, tasks=finished)
        self.assertEqual(status["waiting_on"]["match"]["task_id"], "g2")

    def test_approving_or_rejecting_a_gate_early_is_refused(self):
        from ts_harness.workflows.engine import WorkflowEngine
        from ts_harness.workflows.hooks import build_default_registry

        client = mock.MagicMock()
        client.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": TASKS}
        engine = WorkflowEngine(client=client, registry=build_default_registry())
        with mock.patch.object(engine.registry, "invoke") as invoke:
            with self.assertRaisesRegex(GateNotReady, "approve foundry is not ready yet: design blueprints has not finished"):
                engine.handle_approve(workflow_id="wf", gate_task=TASKS[3], actor="a")
            with self.assertRaises(GateNotReady):
                engine.handle_reject(workflow_id="wf", gate_task=TASKS[5], comments="no", actor="a")
        invoke.assert_not_called()
        client.approve_task.assert_not_called()
        client.reject_task.assert_not_called()

    def test_a_second_approval_of_a_decided_gate_does_nothing(self):
        from ts_harness.workflows.engine import WorkflowEngine
        from ts_harness.workflows.hooks import build_default_registry

        client = mock.MagicMock()
        client.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": TASKS}
        engine = WorkflowEngine(client=client, registry=build_default_registry())
        stale = dict(TASKS[1], approval_status="pending")  # what the second click still saw
        with mock.patch.object(engine.registry, "invoke") as invoke:
            result = engine.handle_approve(workflow_id="wf", gate_task=stale, actor="a")
        self.assertEqual(result, {"status": "already_decided", "task_id": "g1", "approval_status": "approved"})
        invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
