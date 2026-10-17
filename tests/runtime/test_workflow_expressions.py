"""Tests for workflow expression token resolver."""

from __future__ import annotations

import unittest

from ts_harness.workflows.expressions import resolve_input_map, resolve_token


class WorkflowExpressionTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [
            {
                "id": "draft-1",
                "step_name": "draft_prd",
                "status": "succeeded",
                "output": {"title": "PRD"},
                "created_at": "2026-01-01",
            },
            {
                "id": "rev-1",
                "step_name": "revise_prd",
                "status": "succeeded",
                "output": {"title": "PRD v2"},
                "created_at": "2026-01-02",
            },
        ]
        self.workflow = {"source_channel": "slack:C1:123.456"}

    def test_input_token_reads_the_deliver_task(self):
        tasks = [
            {
                "step_name": "deliver_work_order",
                "status": "succeeded",
                "input": {
                    "work_order_body": "# WO-307\n\n## Files\n- app/shelf_code.py\n",
                    "repo_url": "https://github.com/example-org/test-ts-harness-repo",
                    "base_branch": "main",
                },
                "output": {"pr_url": "https://github.com/example-org/test-ts-harness-repo/pull/20"},
                "created_at": "1",
            }
        ]
        self.assertIn("shelf_code", resolve_token("$input.work_order_body", tasks=tasks))
        self.assertEqual(
            resolve_token("$input.repo_url", tasks=tasks),
            "https://github.com/example-org/test-ts-harness-repo",
        )
        self.assertEqual(resolve_token("$workflow.base_branch", tasks=tasks), "main")

    def test_prior_output(self):
        value = resolve_token("$prior.draft_prd.output", tasks=self.tasks, workflow=self.workflow)
        self.assertEqual(value, {"title": "PRD"})

    def test_source_output(self):
        value = resolve_token(
            "$source.output",
            tasks=self.tasks,
            source_steps=["draft_prd", "revise_prd"],
        )
        self.assertEqual(value, {"title": "PRD v2"})

    def test_revision_count(self):
        value = resolve_token("$runtime.revision_count:revise_prd", tasks=self.tasks)
        self.assertEqual(value, 2)

    def test_reject_comments(self):
        value = resolve_token("$reject.comments", tasks=self.tasks, reject_comments="add mobile")
        self.assertEqual(value, "add mobile")

    def test_binding_key(self):
        value = resolve_token("$workflow.binding_key", tasks=self.tasks, workflow=self.workflow)
        self.assertEqual(value, "slack:C1:123.456")

    def test_revise_id_extra(self):
        value = resolve_token("$revise.id", tasks=self.tasks, extras={"revise": {"id": "task-99"}})
        self.assertEqual(value, "task-99")

    def test_re_gate_id_extra(self):
        value = resolve_token(
            "$re_gate.id",
            tasks=self.tasks,
            extras={"re_gate": {"id": "qa-2"}},
        )
        self.assertEqual(value, "qa-2")

    def test_approved_artifact_prefers_generic_extras(self):
        artifact = {"summary": "approved doc"}
        value = resolve_token(
            "$workflow.approved_artifact",
            tasks=self.tasks,
            extras={"approved_artifact": artifact, "approved_prd": {"summary": "legacy"}},
        )
        self.assertEqual(value, artifact)

    def test_approved_prd_legacy_token_still_resolves(self):
        artifact = {"summary": "legacy approved"}
        value = resolve_token(
            "$workflow.approved_prd",
            tasks=self.tasks,
            extras={"approved_artifact": artifact},
        )
        self.assertEqual(value, artifact)

    def test_approved_prd_resolves_from_seeded_task_input(self):
        artifact = {"summary": "approved PRD"}
        value = resolve_token(
            "$workflow.approved_prd",
            tasks=[
                {
                    "step_name": "refine_requirements",
                    "input": {"approved_prd": artifact},
                },
            ],
        )
        self.assertEqual(value, artifact)

    def test_work_order_id_resolves_from_prepare_output(self):
        value = resolve_token(
            "$workflow.work_order_id",
            tasks=[
                {
                    "step_name": "prepare_work_order_delivery",
                    "status": "succeeded",
                    "output": {
                        "next_work_order_id": "WO-001",
                        "work_order_ref": "WO-001",
                    },
                    "created_at": "2026-01-03",
                },
            ],
        )
        self.assertEqual(value, "WO-001")

    def test_work_order_ref_resolves_from_extras(self):
        value = resolve_token(
            "$workflow.work_order_ref",
            tasks=[],
            extras={"work_order_ref": "WO-002"},
        )
        self.assertEqual(value, "WO-002")

    def test_prior_refinery_artifacts_resolve_in_input_map(self):
        resolved = resolve_input_map(
            {
                "prior_refinery": "$prior.refine_requirements.output.artifacts",
            },
            tasks=[
                {
                    "step_name": "refine_requirements",
                    "status": "succeeded",
                    "output": {
                        "artifacts": {
                            "frds_markdown": "# FRD-001",
                        },
                    },
                },
            ],
        )
        self.assertEqual(resolved["prior_refinery"]["frds_markdown"], "# FRD-001")

    def test_input_map(self):
        resolved = resolve_input_map(
            {
                "comments": "$reject.comments",
                "prior": "$source.output",
            },
            tasks=self.tasks,
            reject_comments="fix scope",
            source_steps=["draft_prd", "revise_prd"],
        )
        self.assertEqual(resolved["comments"], "fix scope")
        self.assertEqual(resolved["prior"], {"title": "PRD v2"})


if __name__ == "__main__":
    unittest.main()
