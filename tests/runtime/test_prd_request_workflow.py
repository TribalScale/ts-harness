"""Tests for prd_request workflow skeleton v1.1."""

from __future__ import annotations

import unittest

from ts_harness.workflows.materialize import load_skeleton, materialize_skeleton_tasks


class PrdRequestWorkflowTests(unittest.TestCase):
    def test_prd_request_skeleton_v1_1(self):
        skeleton = load_skeleton("prd_request")
        self.assertEqual(skeleton.definition_version, "1.4.0")
        stage_names = [stage.name for stage in skeleton.stages]
        self.assertEqual(
            stage_names,
            ["intake", "await_intake_answers", "draft_prd", "approve_prd"],
        )
        self.assertNotIn("delivery_planning", stage_names)

    def test_intake_wait_stage_blocks_on_channel_response(self):
        skeleton = load_skeleton("prd_request")
        await_stage = skeleton.stage("await_intake_answers")
        self.assertEqual(await_stage.wait_external.get("event_type"), "channel.intake_response")
        self.assertEqual((await_stage.on_complete or {}).get("downstream"), ["draft_prd"])

    def test_draft_prd_hydrates_from_intake_wait_output(self):
        skeleton = load_skeleton("prd_request")
        draft = skeleton.stage("draft_prd")
        self.assertEqual(draft.depends_on, ["await_intake_answers"])
        self.assertIn("clarified_requirements", draft.input_map)

    def test_approve_prd_has_mark_artifact_approved_hook(self):
        skeleton = load_skeleton("prd_request")
        approve = skeleton.stage("approve_prd")
        self.assertEqual((approve.on_complete or {}).get("hook"), "mark_artifact_approved")
        self.assertEqual((approve.on_reject or {}).get("hook"), "revise_on_reject")

    def test_materialize_prd_request_tasks(self):
        skeleton = load_skeleton("prd_request")
        tasks = materialize_skeleton_tasks(skeleton, workflow_id="wf-prd")
        self.assertEqual(len(tasks), 4)
        approve = next(task for task in tasks if task["step_name"] == "approve_prd")
        self.assertEqual(approve["depends_on"], ["draft_prd"])
        self.assertTrue(approve.get("approval_required"))


if __name__ == "__main__":
    unittest.main()
