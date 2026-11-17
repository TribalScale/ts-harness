"""Found in the end-to-end rehearsal: the planner's bullet dependencies were ignored, and a
cancelled work order counted as delivered."""

from __future__ import annotations

import unittest
from unittest import mock

from ts_harness.implementation.work_order_context import parse_work_order_dependencies, select_ready_work_order_ids
from ts_harness.workflows import work_order_waves
from ts_harness.workflows.work_order_waves import child_state, wave_result

PLANNER_MARKDOWN = """# WO-001 Discussion Category Attribute
## Context Graph
- Depends on: none

# WO-002 Category Selection in the Create Discussion Drawer
## Context Graph
- Depends on: WO-001
- Design: D2

# WO-003 Category Badge Column
- **Depends on:** WO-001

# WO-005 Filter Bar
## Context Graph
- Depends on: WO-004, WO-002, WO-003

# WO-004 Mock API Filter
| Field | Value |
|---|---|
| Depends on | WO-001 |
"""


class PlannerDependencyTests(unittest.TestCase):
    def test_bullet_and_table_dependencies_are_both_read(self):
        deps = parse_work_order_dependencies(PLANNER_MARKDOWN)
        self.assertEqual(
            deps,
            {"WO-001": [], "WO-002": ["WO-001"], "WO-003": ["WO-001"], "WO-005": ["WO-004", "WO-002", "WO-003"], "WO-004": ["WO-001"]},
        )

    def test_first_wave_waits_for_the_foundation(self):
        ids = ["WO-001", "WO-002", "WO-003", "WO-004", "WO-005"]
        deps = parse_work_order_dependencies(PLANNER_MARKDOWN)
        self.assertEqual(select_ready_work_order_ids(ids, [], deps), ["WO-001"])
        self.assertEqual(select_ready_work_order_ids(ids, ["WO-001"], deps), ["WO-002", "WO-003", "WO-004"])
        self.assertEqual(select_ready_work_order_ids(ids, ["WO-001", "WO-002", "WO-003", "WO-004"], deps), ["WO-005"])


def task(step, status, **extra):
    return {"step_name": step, "status": status, **extra}


class CancelledChildTests(unittest.TestCase):
    def test_a_cancelled_child_is_not_a_delivery(self):
        workflow = {"status": "cancelled", "workflow_type": "deliver_work_order"}
        tasks = [task("deliver_work_order", "cancelled")]
        self.assertEqual(child_state(workflow, tasks), "cancelled")
        # Still a success when only superseded revise-loop tasks were cancelled.
        running = {"status": "succeeded", "workflow_type": "deliver_work_order"}
        self.assertEqual(child_state(running, [task("qa_gate", "cancelled"), task("qa_gate", "succeeded"), task("await_human_merge", "succeeded")]), "succeeded")

    def test_wave_leaves_cancelled_work_orders_undone(self):
        payloads = {
            "wf-1": ({"status": "succeeded", "workflow_type": "deliver_work_order"},
                     [task("deliver_work_order", "succeeded", output={"pr_url": "https://github.com/o/r/pull/1"}),
                      task("await_human_merge", "succeeded", output={"merge_sha": "abc"}, completed_at="2026-11-17T10:00:00Z")]),
            "wf-2": ({"status": "cancelled", "workflow_type": "deliver_work_order"}, [task("deliver_work_order", "cancelled")]),
        }
        with mock.patch.object(work_order_waves, "load_workflow_payload", side_effect=lambda memory, wf: payloads[wf]):
            result = wave_result(None, [{"workflow_id": "wf-1", "work_order_id": "WO-001"}, {"workflow_id": "wf-2", "work_order_id": "WO-002"}])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["output"]["wave_work_order_ids"], ["WO-001"])
        self.assertEqual(result["output"]["cancelled_work_order_ids"], ["WO-002"])
        self.assertEqual(result["output"]["failed_work_order_ids"], [])


if __name__ == "__main__":
    unittest.main()
