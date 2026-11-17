"""The runtime derives program operability fields from the memory API's raw tasks."""

from __future__ import annotations

import unittest

from ts_harness.workflows.operability import with_operability

GATE = {
    "id": "t-gate",
    "step_name": "approve",
    "status": "pending",
    "approval_required": True,
    "approval_status": "pending",
    "created_at": "2026-10-01T00:00:00Z",
}


class WithOperabilityTests(unittest.TestCase):
    def test_derives_fields_and_drops_the_raw_tasks(self):
        status = with_operability(
            {"active_workflow_id": "wf-1", "artifacts": {}, "active_workflow": {"id": "wf-1"}, "active_tasks": [GATE]}
        )
        self.assertEqual(status["waiting_on"]["kind"], "approval")
        self.assertEqual(status["active_step"], "approve")
        self.assertEqual(status["recommended_action"], "approve_or_reject_pending_gate")
        self.assertNotIn("active_tasks", status)
        self.assertNotIn("active_workflow", status)

    def test_no_active_workflow_adds_nothing(self):
        status = with_operability({"status": "succeeded", "active_workflow_id": None, "active_tasks": []})
        self.assertNotIn("waiting_on", status)

    def test_already_derived_status_is_left_alone(self):
        derived = {"active_workflow_id": "wf-1", "waiting_on": {"kind": "external"}}
        self.assertEqual(with_operability(derived), derived)

    def test_children_get_their_waiting_step(self):
        status = with_operability(
            {
                "children": [
                    {"id": "c1", "active_workflow_id": "wf-c", "active_tasks": [GATE], "artifacts": {"x": 1}},
                    {"id": "c2", "status": "succeeded"},
                ]
            }
        )
        first, second = status["children"]
        self.assertEqual((first["waiting_on"]["kind"], first["active_step"]), ("approval", "approve"))
        self.assertNotIn("active_tasks", first)
        self.assertNotIn("artifacts", first)
        self.assertEqual(second, {"id": "c2", "status": "succeeded"})


if __name__ == "__main__":
    unittest.main()
