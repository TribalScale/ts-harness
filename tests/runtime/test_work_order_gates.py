"""Reviewer words at the refinery, foundry and work-order approvals reach the next stage."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.expressions import resolve_input_map
from ts_harness.workflows.hooks import approve_gate, revise_on_reject
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext

WORKFLOW = "work_orders_from_prd"
DOWNSTREAM = ["design_blueprints", "approve_foundry", "plan_work_orders", "approve_work_orders", "prepare_work_order_delivery"]


def _ctx(step: str, *, task: dict, tasks: list | None = None, **extra) -> HookContext:
    skeleton = load_skeleton(WORKFLOW)
    client = MagicMock()
    client.approve_task.return_value = {"status": "succeeded"}
    return HookContext(
        workflow_id="wf-1",
        workflow_type=WORKFLOW,
        tenant_id="default",
        task=task,
        stage=skeleton.stage(step),
        skeleton=skeleton,
        actor="orchestrator",
        memory_client=client,
        tasks=tasks or [],
        workflow={"source_channel": "web:c:1"},
        **extra,
    )


class ApprovalNotesTests(unittest.TestCase):
    def test_a_bare_approval_has_no_notes(self):
        for reply in ("approve", "Approved.", "yes, go ahead", "LGTM thanks", "looks good, continue", ""):
            self.assertEqual(approve_gate.approval_notes(reply), "", reply)
        reply = "Approve. ASM-001 confirmed; use 500 characters for order notes."
        self.assertEqual(approve_gate.approval_notes(reply), reply)

    def test_approving_with_notes_records_them_for_the_next_stage(self):
        gate = {"id": "gate-1", "step_name": "approve_refinery", "input": {}}
        reply = "Approve. ASM-012: use 500 characters. Split the work orders by phase."
        with patch("ts_harness.workflows.hooks.hydrate_input.hydrate_downstream"):
            ctx = _ctx("approve_refinery", task=gate, extras={"decision": {}, "reply_text": reply})
            approve_gate.handle(ctx)
        output = ctx.memory_client.approve_task.call_args.kwargs["output"]
        self.assertEqual(output["reviewer_notes"], reply)
        with patch("ts_harness.workflows.hooks.hydrate_input.hydrate_downstream"):
            plain = _ctx("approve_refinery", task=gate, extras={"decision": {}, "reply_text": "approved"})
            approve_gate.handle(plain)
        self.assertIsNone(plain.memory_client.approve_task.call_args.kwargs["output"])

    def test_foundry_and_planner_read_the_notes(self):
        skeleton = load_skeleton(WORKFLOW)
        tasks = [
            {"id": "r", "step_name": "refine_requirements", "status": "succeeded", "output": {"artifacts": {"a": 1}}, "created_at": "1"},
            {"id": "g1", "step_name": "approve_refinery", "status": "succeeded", "output": {"reviewer_notes": "ASM-012: 500"}, "created_at": "2"},
            {"id": "f", "step_name": "design_blueprints", "status": "succeeded", "output": {"artifacts": {"b": 1}}, "created_at": "3"},
            {"id": "g2", "step_name": "approve_foundry", "status": "succeeded", "output": None, "created_at": "4"},
        ]
        foundry = resolve_input_map(dict(skeleton.stage("design_blueprints").input_map), tasks=tasks, workflow={})
        self.assertEqual(foundry["refinery_reviewer_notes"], "ASM-012: 500")
        planner = resolve_input_map(dict(skeleton.stage("plan_work_orders").input_map), tasks=tasks, workflow={})
        self.assertEqual(planner["refinery_reviewer_notes"], "ASM-012: 500")
        self.assertIsNone(planner["foundry_reviewer_notes"])


class RejectRevisesTests(unittest.TestCase):
    def test_every_gate_revises_its_stage_and_replays_what_follows(self):
        skeleton = load_skeleton(WORKFLOW)
        expected = {
            "approve_refinery": ("refine_requirements", DOWNSTREAM),
            "approve_foundry": ("design_blueprints", DOWNSTREAM[2:]),
            "approve_work_orders": ("plan_work_orders", DOWNSTREAM[4:]),
        }
        for gate, (step, resume) in expected.items():
            config = skeleton.stage(gate).on_reject
            self.assertEqual(config["hook"], "revise_on_reject")
            self.assertEqual(config["revise"]["step_name"], step)
            chain = config["resume_after"]
            self.assertEqual([item["step_name"] for item in chain], resume)
            self.assertEqual([item["step_name"] for item in chain if item.get("approval_required")],
                             [name for name in resume if name.startswith("approve_")])

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_rejecting_refinery_redrafts_it_with_the_comments(self, mock_bulk):
        mock_bulk.side_effect = [["refine-2"], ["gate-2"], *[[f"{name}-2"] for name in DOWNSTREAM]]
        gate = {"id": "gate-1", "step_name": "approve_refinery", "approval_status": "pending", "status": "pending"}
        refinery = {"id": "refine-1", "step_name": "refine_requirements", "status": "succeeded", "created_at": "1",
                    "input": {"approved_prd": "# PRD", "stage": "refinery"}, "output": {"artifacts": {"frds_markdown": "x"}}}
        pending = [{"id": f"{name}-1", "step_name": name, "status": "pending", "created_at": "2"} for name in DOWNSTREAM]
        tasks = [refinery, gate, *pending]
        ctx = _ctx("approve_refinery", task=gate, tasks=tasks, reject_comments="ASM-012: use 500 characters")
        ctx.memory_client.get_workflow.return_value = {"workflow": ctx.workflow, "tasks": tasks}

        revise_on_reject.handle(ctx)

        cancelled = [call.args[0] for call in ctx.memory_client.cancel_task.call_args_list]
        self.assertEqual(cancelled, ["gate-1", *[f"{name}-1" for name in DOWNSTREAM]])
        created = [call.kwargs["tasks"][0] for call in mock_bulk.call_args_list]
        revise, re_gate, *chain = created
        self.assertEqual(revise["step_name"], "refine_requirements")
        self.assertEqual((revise["input"]["stage"], revise["input"]["comments"]), ("refinery", "ASM-012: use 500 characters"))
        self.assertEqual(revise["input"]["prior_artifact"], {"artifacts": {"frds_markdown": "x"}})
        self.assertEqual((re_gate["step_name"], re_gate["depends_on"], re_gate["approval_required"]),
                         ("approve_refinery", ["refine-2"], True))
        self.assertEqual([(task["step_name"], task["approval_required"]) for task in chain],
                         [(name, name.startswith("approve_")) for name in DOWNSTREAM])
        self.assertEqual(chain[0]["depends_on"], ["gate-2"])
        self.assertEqual(chain[1]["depends_on"], ["design_blueprints-2"])


if __name__ == "__main__":
    unittest.main()
