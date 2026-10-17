"""Tests for delivery program fork after PRD."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.delivery_program import (
    DeliveryProgramService,
    resolve_bridge_target_from_text,
    resolve_workflow_type,
)
from ts_harness.workflows.program_materialize import snapshot_edges_from

ensure_scripts_importable()
import program_legacy_compat  # noqa: E402


class DeliveryProgramForkTests(unittest.TestCase):
    def test_prd_has_two_successors(self):
        snap = program_legacy_compat.frozen_legacy_compatibility_topology()
        targets = {str(edge["target"]) for edge in snapshot_edges_from(snap, "prd")}
        self.assertEqual(targets, {"jira", "work_orders"})

    def test_resolve_bridge_target_work_orders(self):
        target = resolve_bridge_target_from_text("generate work orders", "prd_request")
        self.assertEqual(target, "work_orders_from_prd")

    def test_resolve_bridge_target_jira(self):
        target = resolve_bridge_target_from_text("create jira tickets", "prd_request")
        self.assertEqual(target, "jira_from_prd")

    def test_implement_skeleton_by_track(self):
        self.assertEqual(
            resolve_workflow_type("implement", {"planning_track": "work_orders"}),
            "implement_work_orders",
        )
        self.assertEqual(resolve_workflow_type("implement", {}), "implement_delivery")

    def test_advance_from_prd_requires_explicit_target(self):
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": "prd",
                "summary": "Delivery",
                "metadata": {"artifacts": {"approved_prd": {"ok": True}}},
            },
            "workflows": [],
        }
        service = DeliveryProgramService(client=client)
        with self.assertRaises(ValueError):
            service.advance_program(program_id="prog-1", summary="Continue")

    def test_advance_to_work_orders(self):
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": "prd",
                "summary": "Delivery",
                "metadata": {"artifacts": {"approved_prd": {"ok": True}}},
            },
            "workflows": [],
        }
        client.advance_program.return_value = {"current_phase": "work_orders"}
        client.create_workflow.return_value = {"id": "wf-wo"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-1"}

        service = DeliveryProgramService(client=client)
        result = service.advance_program(
            program_id="prog-1",
            target_phase="work_orders",
            summary="Plan work orders",
            artifacts={"approved_prd": {"ok": True}, "planning_track": "work_orders"},
        )
        self.assertEqual(result["target_phase"], "work_orders")
        self.assertEqual(result["workflow_type"], "work_orders_from_prd")
        self.assertEqual(result["first_step"], "refine_requirements")


if __name__ == "__main__":
    unittest.main()
