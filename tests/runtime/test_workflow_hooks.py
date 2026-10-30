"""Fixture-based tests for individual workflow hooks."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.hooks import (
    hydrate_input,
    notify_bound_channel,
    revise_on_reject,
    wait_external,
)
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


class WorkflowHookTests(unittest.TestCase):
    def _ctx(self, **overrides):
        skeleton = load_skeleton("prd_request")
        base = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "tenant_id": "default",
            "task": {"id": "gate-1", "step_name": "approve_prd", "approval_status": "pending"},
            "stage": skeleton.stage("approve_prd"),
            "skeleton": skeleton,
            "actor": "orchestrator",
            "memory_client": MagicMock(),
            "reject_comments": "revise intro",
            "tasks": [
                {
                    "id": "draft-1",
                    "step_name": "draft_prd",
                    "status": "succeeded",
                    "output": {"body": "draft"},
                    "created_at": "2026-01-01",
                },
            ],
            "workflow": {"source_channel": "slack:C1:123.456"},
        }
        base.update(overrides)
        return HookContext(**base)

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_revise_on_reject_hook(self, mock_bulk):
        mock_bulk.side_effect = [["rev-1"], ["gate-2"]]
        ctx = self._ctx()
        ctx.memory_client.get_workflow.return_value = {
            "workflow": ctx.workflow,
            "tasks": ctx.tasks + [ctx.task],
        }
        result = revise_on_reject.handle(ctx)
        self.assertEqual(result["revise_task_id"], "rev-1")
        ctx.memory_client.reject_task.assert_called_once()

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_reject_with_resume_after_replaces_the_pending_downstream_task(self, mock_bulk):
        mock_bulk.side_effect = [["plan-2"], ["gate-2"], ["file-2"]]
        skeleton = load_skeleton("jira_from_prd")
        gate = {"id": "gate-1", "step_name": "approve_plan", "approval_status": "pending", "status": "pending"}
        tasks = [
            {"id": "plan-1", "step_name": "plan_stories", "status": "succeeded", "output": {"plan_markdown": "x"}, "created_at": "2026-01-01"},
            gate,
            {"id": "file-1", "step_name": "file_jira", "status": "pending", "created_at": "2026-01-01"},
        ]
        ctx = self._ctx(
            workflow_type="jira_from_prd",
            skeleton=skeleton,
            stage=skeleton.stage("approve_plan"),
            task=gate,
            tasks=tasks,
            reject_comments="split story 1.2",
        )
        ctx.memory_client.get_workflow.return_value = {"workflow": ctx.workflow, "tasks": tasks}

        result = revise_on_reject.handle(ctx)

        ctx.memory_client.reject_task.assert_called_once()
        cancelled = [call.args[0] for call in ctx.memory_client.cancel_task.call_args_list]
        self.assertEqual(cancelled, ["gate-1", "file-1"])
        revise, re_gate, resume = (call.kwargs["tasks"][0] for call in mock_bulk.call_args_list)
        self.assertEqual(revise["step_name"], "plan_stories")
        self.assertEqual(revise["input"]["comments"], "split story 1.2")
        self.assertEqual((re_gate["step_name"], re_gate["depends_on"]), ("approve_plan", ["plan-2"]))
        self.assertEqual((resume["step_name"], resume["agent_id"], resume["depends_on"]), ("file_jira", "pm-agent", ["gate-2"]))
        self.assertEqual(result["re_gate_task_ids"], ["gate-2"])

    def test_hydrate_input_skips_without_map(self):
        skeleton = load_skeleton("implement_ticket")
        ctx = self._ctx(
            skeleton=skeleton,
            stage=skeleton.stage("implement"),
            task={"id": "t1", "step_name": "implement", "input": {}},
        )
        result = hydrate_input.handle(ctx)
        self.assertEqual(result["status"], "skipped")

    def test_program_artifact_extras_include_related_context(self):
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "metadata": {
                    "artifacts": {
                        "related_program_context": {"project_id": "TFS", "sources": []},
                    }
                }
            }
        }
        extras = hydrate_input._program_artifact_extras(client, {"program_id": "prog-1"})
        self.assertEqual(extras["related_program_context"]["project_id"], "TFS")

    def test_hydrate_downstream_uses_dispatch_output_before_db_persist(self):
        skeleton = load_skeleton("deliver_work_order")
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-1", "program_id": None},
            "tasks": [
                {
                    "id": "deliver-1",
                    "step_name": "deliver_work_order",
                    "status": "running",
                    "output": {},
                    "created_at": "2026-01-01",
                },
                {
                    "id": "qa-1",
                    "step_name": "qa_gate",
                    "status": "pending",
                    "input": {"pr_url": None, "work_order_ref": "WO-001"},
                    "created_at": "2026-01-02",
                },
            ],
        }
        client.patch_task_input.side_effect = lambda task_id, merged, actor=None: {
            "id": task_id,
            "input": merged,
        }
        ctx = self._ctx(
            skeleton=skeleton,
            stage=skeleton.stage("deliver_work_order"),
            task={
                "id": "deliver-1",
                "step_name": "deliver_work_order",
                "status": "running",
                "output": {},
            },
            dispatch_result={
                "output": {
                    "pr_url": "https://github.com/org/app/pull/7",
                    "work_order_id": "WO-002",
                }
            },
            memory_client=client,
        )

        results = hydrate_input.hydrate_downstream(ctx, ["qa_gate"])

        self.assertEqual(len(results), 1)
        patched = client.patch_task_input.call_args.args[1]
        self.assertEqual(patched["pr_url"], "https://github.com/org/app/pull/7")
        self.assertEqual(patched["work_order_id"], "WO-002")
        self.assertEqual(patched["work_order_ref"], "WO-002")

    def test_hydrate_downstream_patches_latest_pending_qa_gate(self):
        skeleton = load_skeleton("deliver_work_order")
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-1", "program_id": None},
            "tasks": [
                {
                    "id": "deliver-2",
                    "step_name": "deliver_work_order",
                    "status": "running",
                    "output": {},
                    "created_at": "2026-01-03",
                },
                {
                    "id": "qa-1",
                    "step_name": "qa_gate",
                    "status": "cancelled",
                    "input": {"pr_url": "https://github.com/org/app/pull/8"},
                    "created_at": "2026-01-02",
                },
                {
                    "id": "qa-2",
                    "step_name": "qa_gate",
                    "status": "pending",
                    "input": {"pr_url": None, "work_order_ref": "WO-003"},
                    "created_at": "2026-01-04",
                },
            ],
        }
        client.patch_task_input.side_effect = lambda task_id, merged, actor=None: {
            "id": task_id,
            "input": merged,
        }
        ctx = self._ctx(
            skeleton=skeleton,
            stage=skeleton.stage("deliver_work_order"),
            task={
                "id": "deliver-2",
                "step_name": "deliver_work_order",
                "status": "running",
                "output": {},
            },
            dispatch_result={
                "output": {
                    "pr_url": "https://github.com/org/app/pull/8",
                    "work_order_id": "WO-003",
                }
            },
            memory_client=client,
        )

        results = hydrate_input.hydrate_downstream(ctx, ["qa_gate"])

        self.assertEqual(len(results), 1)
        self.assertEqual(client.patch_task_input.call_args.args[0], "qa-2")
        patched = client.patch_task_input.call_args.args[1]
        self.assertEqual(patched["pr_url"], "https://github.com/org/app/pull/8")
        self.assertEqual(patched["work_order_id"], "WO-003")

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_revise_on_block_spawns_deliver_checks_qa_and_merge(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-2"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        skeleton = load_skeleton("deliver_work_order")
        qa_stage = skeleton.stage("qa_gate")
        deliver = {
            "id": "deliver-1",
            "step_name": "deliver_work_order",
            "status": "succeeded",
            "input": {
                "work_order_id": "WO-003",
                "work_order_body": "# WO-003 Checkers",
                "repo_url": "https://github.com/org/app.git",
            },
            "output": {
                "pr_url": "https://github.com/org/app/pull/8",
                "work_order_id": "WO-003",
            },
            "created_at": "2026-01-01",
        }
        gate = {
            "id": "qa-1",
            "step_name": "qa_gate",
            "status": "blocked",
            "output": {"status": "failed", "result": {"summary": "No evidence"}},
            "created_at": "2026-01-02",
        }
        merge = {
            "id": "merge-1",
            "step_name": "await_human_merge",
            "status": "pending",
            "created_at": "2026-01-02",
        }
        ctx = self._ctx(
            workflow_type="deliver_work_order",
            skeleton=skeleton,
            stage=qa_stage,
            task=gate,
            runner_event="blocked",
            reject_comments="No evidence of implementation or testing",
            tasks=[deliver, gate, merge],
        )
        ctx.memory_client.get_workflow.return_value = {
            "workflow": ctx.workflow,
            "tasks": [deliver, gate, merge],
        }
        ctx.memory_client.cancel_task.return_value = {"status": "cancelled"}

        result = revise_on_reject.handle(ctx)

        self.assertEqual(result["status"], "revising")
        self.assertEqual(result["revise_task_id"], "deliver-2")
        self.assertEqual(result["re_gate_task_ids"], ["checks-2"])
        self.assertEqual(
            result["resume_after_task_ids"],
            ["precheck-2", "review-2", "finalize-2", "qa-2", "fresh-2", "merge-2"],
        )
        ctx.memory_client.reject_task.assert_not_called()
        cancelled = [call.args[0] for call in ctx.memory_client.cancel_task.call_args_list]
        self.assertIn("qa-1", cancelled)
        self.assertIn("merge-1", cancelled)
        deliver_payload = mock_bulk.call_args_list[0].kwargs["tasks"][0]
        self.assertEqual(deliver_payload["step_name"], "deliver_work_order")
        self.assertEqual(deliver_payload["input"]["work_order_id"], "WO-003")
        self.assertEqual(
            deliver_payload["input"]["qa_feedback"],
            "No evidence of implementation or testing",
        )
        self.assertEqual(
            deliver_payload["input"]["pr_url"],
            "https://github.com/org/app/pull/8",
        )
        self.assertTrue(deliver_payload.get("contract"))
        checks_payload = mock_bulk.call_args_list[1].kwargs["tasks"][0]
        self.assertEqual(checks_payload["step_name"], "await_pr_checks")
        self.assertEqual(checks_payload["depends_on"], ["deliver-2"])
        self.assertFalse(checks_payload.get("approval_required"))
        chain = [call.kwargs["tasks"][0] for call in mock_bulk.call_args_list[2:]]
        self.assertEqual(
            [(task["step_name"], task["depends_on"]) for task in chain],
            [
                ("review_precheck", ["checks-2"]),
                ("code_review", ["precheck-2"]),
                ("finalize_code_review", ["review-2"]),
                ("qa_gate", ["finalize-2"]),
                ("ensure_fresh", ["qa-2"]),
                ("await_human_merge", ["fresh-2"]),
            ],
        )
        self.assertEqual(chain[1]["agent_id"], "review-agent")
        self.assertTrue(chain[1].get("contract"))
        qa_payload = chain[3]
        self.assertFalse(qa_payload.get("approval_required"))
        self.assertTrue(qa_payload.get("contract"))
        merge_payload = mock_bulk.call_args_list[7].kwargs["tasks"][0]
        self.assertEqual(merge_payload["step_name"], "await_human_merge")
        self.assertEqual(merge_payload["depends_on"], ["fresh-2"])

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_revise_on_block_stops_at_revision_limit(self, mock_bulk):
        skeleton = load_skeleton("deliver_work_order")
        delivers = [
            {
                "id": f"deliver-{i}",
                "step_name": "deliver_work_order",
                "status": "succeeded",
                "input": {"work_order_id": "WO-003"},
                "output": {"pr_url": "https://github.com/org/app/pull/8"},
                "created_at": f"2026-01-0{i}",
            }
            for i in range(1, 5)
        ]
        gate = {
            "id": "qa-4",
            "step_name": "qa_gate",
            "status": "blocked",
            "created_at": "2026-01-05",
        }
        ctx = self._ctx(
            workflow_type="deliver_work_order",
            skeleton=skeleton,
            stage=skeleton.stage("qa_gate"),
            task=gate,
            runner_event="blocked",
            reject_comments="still failing",
            tasks=delivers + [gate],
        )
        ctx.memory_client.get_workflow.return_value = {
            "workflow": ctx.workflow,
            "tasks": delivers + [gate],
        }

        result = revise_on_reject.handle(ctx)

        self.assertEqual(result["status"], "revision_limit")
        mock_bulk.assert_not_called()
        ctx.memory_client.cancel_task.assert_not_called()

    def _review_block_ctx(self, tasks, *, grant=None):
        skeleton = load_skeleton("deliver_work_order")
        gate = next(t for t in tasks if t["step_name"] == "finalize_code_review")
        ctx = self._ctx(
            workflow_type="deliver_work_order",
            skeleton=skeleton,
            stage=skeleton.stage("finalize_code_review"),
            task=gate,
            runner_event="blocked",
            reject_comments="fix the persistence bug",
            tasks=tasks,
            extras={"error": "barrier_blocked: code_review_blocked: 1 blocking findings", **({"grant": grant} if grant else {})},
        )
        ctx.memory_client.get_workflow.return_value = {"workflow": ctx.workflow, "tasks": tasks}
        return ctx

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_human_grant_revises_past_the_limit_and_starts_a_new_budget(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-5"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        gate = {"id": "finalize-4", "step_name": "finalize_code_review", "status": "blocked", "created_at": "2026-01-05"}
        tasks = [self._deliver(i) for i in range(1, 5)] + [gate]
        self.assertEqual(revise_on_reject.handle(self._review_block_ctx(tasks))["status"], "revision_limit")

        result = revise_on_reject.handle(self._review_block_ctx(tasks, grant={"actor": "alice"}))

        self.assertEqual(result["status"], "revising")
        self.assertEqual((result["revision_number"], result["max_revisions"]), (1, 3))
        payload = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertEqual(payload["_revision_grant"], {"actor": "alice"})
        self.assertNotIn("_revision_budget", payload)

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_human_grant_lifts_the_file_limit_of_the_review(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-5"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        gate = {
            "id": "finalize-4",
            "step_name": "finalize_code_review",
            "status": "blocked",
            "created_at": "2026-01-05",
            "output": {"revision_files": ["src/PortfolioDashboard.jsx"]},
        }
        tasks = [self._deliver(i, revision_files=["src/PortfolioDashboard.jsx"]) for i in range(1, 5)] + [gate]

        revise_on_reject.handle(self._review_block_ctx(tasks, grant={"actor": "alice"}))

        self.assertNotIn("revision_files", mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"])

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_review_revision_keeps_the_files_its_findings_named(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-2"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        gate = {
            "id": "finalize-1",
            "step_name": "finalize_code_review",
            "status": "blocked",
            "created_at": "2026-01-02",
            "output": {"revision_files": ["src/PortfolioDashboard.jsx"]},
        }

        revise_on_reject.handle(self._review_block_ctx([self._deliver(1), gate]))

        payload = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertEqual(payload["revision_files"], ["src/PortfolioDashboard.jsx"])

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_qa_revision_does_not_inherit_the_review_file_limit(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-3"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        skeleton = load_skeleton("deliver_work_order")
        gate = {"id": "qa-2", "step_name": "qa_gate", "status": "blocked", "created_at": "2026-01-03"}
        tasks = [self._deliver(1), self._deliver(2, revision_files=["src/PortfolioDashboard.jsx"]), gate]
        ctx = self._ctx(
            workflow_type="deliver_work_order",
            skeleton=skeleton,
            stage=skeleton.stage("qa_gate"),
            task=gate,
            runner_event="blocked",
            reject_comments="wire it into App.jsx",
            tasks=tasks,
        )
        ctx.memory_client.get_workflow.return_value = {"workflow": ctx.workflow, "tasks": tasks}

        revise_on_reject.handle(ctx)

        payload = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertNotIn("revision_files", payload)
        self.assertEqual(payload["qa_feedback"], "wire it into App.jsx")

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_revisions_after_a_grant_get_the_original_limit(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-8"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        gate = {"id": "finalize-7", "step_name": "finalize_code_review", "status": "blocked", "created_at": "2026-01-08"}
        granted = self._deliver(5, _revision_grant={"actor": "alice"})
        tasks = [self._deliver(i) for i in range(1, 5)] + [granted, self._deliver(6), gate]

        result = revise_on_reject.handle(self._review_block_ctx(tasks))

        self.assertEqual(result["status"], "revising")
        self.assertEqual((result["revision_number"], result["max_revisions"]), (3, 3))
        self.assertNotIn("_revision_grant", mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"])

        mock_bulk.reset_mock()
        tasks = tasks[:-1] + [self._deliver(7), gate]
        result = revise_on_reject.handle(self._review_block_ctx(tasks))
        self.assertEqual(result["status"], "revision_limit")
        mock_bulk.assert_not_called()

    def _budget_ctx(self, tasks, *, budget="refresh", max_revisions=2):
        import dataclasses

        skeleton = load_skeleton("deliver_work_order")
        qa_stage = skeleton.stage("qa_gate")
        on_block = {
            **qa_stage.on_block,
            "max_revisions": max_revisions,
            "revise": {
                **qa_stage.on_block["revise"],
                "budget": budget,
                "static_input": {"refresh": True},
                "input_map": {"pr_url": "$prior.deliver_work_order.output.pr_url"},
            },
        }
        stage = dataclasses.replace(qa_stage, on_block=on_block)
        gate = next(t for t in tasks if t["step_name"] == "qa_gate")
        ctx = self._ctx(
            workflow_type="deliver_work_order",
            skeleton=skeleton,
            stage=stage,
            task=gate,
            runner_event="blocked",
            reject_comments="base moved",
            tasks=tasks,
        )
        ctx.memory_client.get_workflow.return_value = {"workflow": ctx.workflow, "tasks": tasks}
        return ctx

    def _deliver(self, i, **input_extra):
        return {
            "id": f"deliver-{i}",
            "step_name": "deliver_work_order",
            "status": "succeeded",
            "input": {"work_order_id": "WO-003", "qa_feedback": "old", **input_extra},
            "output": {"pr_url": "https://github.com/org/app/pull/8"},
            "created_at": f"2026-01-0{i}",
        }

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_budgeted_revision_ignores_default_revisions_and_merges_static_input(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-4"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        gate = {"id": "qa-3", "step_name": "qa_gate", "status": "blocked", "created_at": "2026-01-04"}
        tasks = [self._deliver(1), self._deliver(2), self._deliver(3), gate]
        result = revise_on_reject.handle(self._budget_ctx(tasks))
        self.assertEqual(result["status"], "revising")
        self.assertEqual(result["max_revisions"], 2)
        payload = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertEqual(payload["_revision_budget"], "refresh")
        self.assertTrue(payload["refresh"])
        self.assertEqual(payload["revision_number"], 1)
        self.assertNotIn("qa_feedback", payload)

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_budgeted_revision_limit_counts_only_its_budget(self, mock_bulk):
        gate = {"id": "qa-3", "step_name": "qa_gate", "status": "blocked", "created_at": "2026-01-04"}
        tasks = [
            self._deliver(1),
            self._deliver(2, _revision_budget="refresh"),
            self._deliver(3, _revision_budget="refresh"),
            gate,
        ]
        result = revise_on_reject.handle(self._budget_ctx(tasks))
        self.assertEqual(result["status"], "revision_limit")
        mock_bulk.assert_not_called()

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_default_budget_ignores_refresh_revisions(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-5"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        gate = {"id": "qa-3", "step_name": "qa_gate", "status": "blocked", "created_at": "2026-01-05"}
        tasks = [
            self._deliver(1),
            self._deliver(2, _revision_budget="refresh"),
            self._deliver(3, _revision_budget="refresh"),
            self._deliver(4, _revision_budget="refresh"),
            gate,
        ]
        result = revise_on_reject.handle(self._budget_ctx(tasks, budget="", max_revisions=1))
        self.assertEqual(result["status"], "revising")
        payload = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertNotIn("_revision_budget", payload)

    def _merge_wait_ctx(self, tasks, error, comments=""):
        skeleton = load_skeleton("deliver_work_order")
        gate = next(t for t in tasks if t["step_name"] == "await_human_merge")
        ctx = self._ctx(
            workflow_type="deliver_work_order",
            skeleton=skeleton,
            stage=skeleton.stage("await_human_merge"),
            task=gate,
            runner_event="blocked",
            reject_comments=comments,
            tasks=tasks,
            extras={"error": error},
        )
        ctx.memory_client.get_workflow.return_value = {"workflow": ctx.workflow, "tasks": tasks}
        return ctx

    def test_block_cases_pick_the_first_matching_prefix(self):
        config = {
            "max_revisions": 5,
            "revise": {"step_name": "a"},
            "cases": [
                {"error_prefix": "x.closed", "action": "stop"},
                {"error_prefix": "x.changes", "max_revisions": 3, "revise": {"step_name": "b"}},
            ],
        }
        self.assertEqual(
            revise_on_reject.select_block_case(dict(config), "x.changes by bob"),
            {"max_revisions": 3, "revise": {"step_name": "b"}},
        )
        self.assertEqual(revise_on_reject.select_block_case(dict(config), "x.closed")["action"], "stop")
        self.assertEqual(
            revise_on_reject.select_block_case(dict(config), "external_blocked:github.merge base_advanced"),
            {"max_revisions": 5, "revise": {"step_name": "a"}},
        )

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_changes_requested_revises_with_its_own_human_budget(self, mock_bulk):
        mock_bulk.side_effect = [["deliver-5"], ["checks-2"], ["precheck-2"], ["review-2"], ["finalize-2"], ["qa-2"], ["fresh-2"], ["merge-2"]]
        gate = {"id": "merge-1", "step_name": "await_human_merge", "status": "blocked", "created_at": "2026-01-05"}
        tasks = [
            self._deliver(1),
            self._deliver(2),
            self._deliver(3, _revision_budget="refresh"),
            self._deliver(4, _revision_budget="human"),
            gate,
        ]
        ctx = self._merge_wait_ctx(
            tasks,
            "external_blocked:github.pr_changes_requested by alice",
            comments="Human reviewer alice requested changes on the pull request:\nRename the flag.",
        )

        result = revise_on_reject.handle(ctx)

        self.assertEqual(result["status"], "revising")
        self.assertEqual(result["max_revisions"], 3)
        payload = mock_bulk.call_args_list[0].kwargs["tasks"][0]["input"]
        self.assertEqual(payload["_revision_budget"], "human")
        self.assertEqual(payload["revision_number"], 2)
        self.assertIn("Rename the flag.", payload["review_feedback"])
        self.assertNotIn("refresh", payload)
        self.assertEqual(payload["pr_url"], "https://github.com/org/app/pull/8")
        self.assertEqual(payload["human_change_requests"], [])
        tasks[3]["input"]["review_feedback"] = "Human reviewer bob requested changes:\nUse --dry-run."
        mock_bulk.side_effect = [["deliver-6"], ["checks-3"], ["precheck-3"], ["review-3"], ["finalize-3"], ["qa-3"], ["fresh-3"], ["merge-3"]]
        revise_on_reject.handle(ctx)
        self.assertEqual(
            mock_bulk.call_args_list[8].kwargs["tasks"][0]["input"]["human_change_requests"],
            ["Human reviewer bob requested changes:\nUse --dry-run."],
        )

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_changes_requested_stops_after_three_human_revisions(self, mock_bulk):
        gate = {"id": "merge-1", "step_name": "await_human_merge", "status": "blocked", "created_at": "2026-01-05"}
        tasks = [self._deliver(1)] + [self._deliver(i, _revision_budget="human") for i in (2, 3, 4)] + [gate]

        result = revise_on_reject.handle(self._merge_wait_ctx(tasks, "external_blocked:github.pr_changes_requested by alice"))

        self.assertEqual(result["status"], "revision_limit")
        self.assertEqual(result["max_revisions"], 3)
        mock_bulk.assert_not_called()

    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_closed_pr_stops_without_revising(self, mock_bulk):
        gate = {"id": "merge-1", "step_name": "await_human_merge", "status": "blocked", "created_at": "2026-01-05"}
        ctx = self._merge_wait_ctx([self._deliver(1), gate], "external_blocked:github.pr_closed by alice")

        result = revise_on_reject.handle(ctx)

        self.assertEqual(result["status"], "stopped")
        mock_bulk.assert_not_called()
        ctx.memory_client.cancel_task.assert_not_called()

    @patch("ts_harness.agent.transcript.get_transcript_store")
    @patch("ts_harness.gateway.workflow_presentation._session_for_workflow")
    @patch("ts_harness.notifications.slack.SlackNotifier")
    def test_notify_bound_channel_writes_web_transcript(
        self,
        mock_notifier_cls,
        mock_session_for_workflow,
        mock_transcript_store,
    ):
        from ts_harness.agent.sessions import SessionRecord

        mock_session_for_workflow.return_value = SessionRecord(
            session_id="sess-web",
            routing_key="web:abc",
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
        )
        ctx = self._ctx(
            runner_event="blocked",
            extras={"error": "git clone failed: repository not found"},
            task={"id": "t-join", "step_name": "join_wave", "agent_id": "orchestrator"},
            workflow={"source_channel": "web:thread-1"},
        )
        result = notify_bound_channel.handle(ctx)
        self.assertEqual(result["status"], "notified")
        self.assertEqual(result["mode"], "web_transcript")
        mock_notifier_cls.assert_not_called()
        messages = mock_transcript_store.return_value.append_messages.call_args.args[1]
        self.assertIn("repository not found", messages[0]["content"])
        self.assertIn("will not retry automatically", messages[0]["content"])
        self.assertNotIn("blocked —", messages[0]["content"])

    @patch("ts_harness.agent.transcript.get_transcript_store")
    @patch("ts_harness.gateway.workflow_presentation._session_for_workflow")
    @patch("ts_harness.notifications.slack.SlackNotifier")
    def test_notify_bound_channel_skips_status_lines_on_web(
        self,
        mock_notifier_cls,
        mock_session_for_workflow,
        mock_transcript_store,
    ):
        from ts_harness.agent.sessions import SessionRecord

        mock_session_for_workflow.return_value = SessionRecord(
            session_id="sess-web",
            routing_key="web:abc",
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
        )
        ctx = self._ctx(
            runner_event="awaiting_external",
            extras={"error": "awaiting_external:channel.intake_response"},
            task={"id": "t-intake", "step_name": "intake", "agent_id": "orchestrator"},
            workflow={"source_channel": "web:thread-1"},
        )
        result = notify_bound_channel.handle(ctx)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not a chat message")
        mock_transcript_store.return_value.append_messages.assert_not_called()
        mock_notifier_cls.assert_not_called()

    @patch("ts_harness.notifications.slack.SlackNotifier")
    def test_notify_bound_channel(self, mock_notifier_cls):
        mock_notifier_cls.return_value.notify_workflow_event.return_value = None
        ctx = self._ctx(runner_event="failed", extras={"error": "boom"})
        result = notify_bound_channel.handle(ctx)
        self.assertEqual(result["status"], "notified")

    @patch("ts_harness.notifications.slack.SlackNotifier")
    def test_notify_bound_channel_posts_channel_text(self, mock_notifier_cls):
        mock_notifier_cls.return_value.post_thread_text.return_value = {"ok": True}
        ctx = self._ctx(
            runner_event="completed",
            extras={"channel_text": "All work orders are delivered. Implement is complete."},
        )
        result = notify_bound_channel.handle(ctx)
        self.assertEqual(result["status"], "notified")
        mock_notifier_cls.return_value.notify_workflow_event.assert_not_called()
        kwargs = mock_notifier_cls.return_value.post_thread_text.call_args.kwargs
        self.assertEqual(kwargs["channel"], "C1")
        self.assertEqual(kwargs["thread_ts"], "123.456")
        self.assertIn("Implement is complete", kwargs["text"])

    @patch("ts_harness.notifications.slack.SlackNotifier")
    def test_notify_bound_channel_terminal_block_includes_guidance(self, mock_notifier_cls):
        mock_notifier_cls.return_value.notify_workflow_event.return_value = None
        ctx = self._ctx(
            runner_event="blocked",
            extras={"error": "missing required artifact keys: requirements_markdown"},
            task={"id": "t-refine", "step_name": "refine_requirements", "agent_id": "pm-agent"},
        )
        result = notify_bound_channel.handle(ctx)
        self.assertEqual(result["status"], "notified")
        kwargs = mock_notifier_cls.return_value.notify_workflow_event.call_args.kwargs
        self.assertIn("will not retry automatically", kwargs.get("error") or "")
        self.assertIn("refine_requirements", kwargs.get("error") or "")

    @patch("ts_harness.notifications.slack.SlackNotifier")
    def test_notify_bound_channel_revise_loop_skips_terminal_guidance(
        self, mock_notifier_cls
    ):
        mock_notifier_cls.return_value.notify_workflow_event.return_value = None
        ctx = self._ctx(
            runner_event="blocked",
            extras={
                "error": "QA rejected the PR; sending it back to OpenCode (revision 2/3)."
            },
            task={"id": "qa-1", "step_name": "qa_gate", "agent_id": "qa-agent"},
        )
        result = notify_bound_channel.handle(ctx)
        self.assertEqual(result["status"], "notified")
        kwargs = mock_notifier_cls.return_value.notify_workflow_event.call_args.kwargs
        self.assertNotIn("will not retry automatically", kwargs.get("error") or "")
        self.assertIn("sending it back to OpenCode", kwargs.get("error") or "")

    def test_failed_pr_checks_block_instead_of_resuming(self):
        skeleton = load_skeleton("deliver_work_order")
        wait_task = {
            "id": "wait-1",
            "step_name": "await_pr_checks",
            "status": "blocked",
            "error": "awaiting_external:github.pr_checks",
            "expected_event_type": "github.pr_checks",
        }
        output = {"checks_state": "failure", "failed_checks": ["lint", "dependency-audit"], "checks": {}}
        ctx = self._ctx(
            skeleton=skeleton,
            stage=skeleton.stage("await_pr_checks"),
            workflow_type="deliver_work_order",
            extras={"event_type": "github.pr_checks", "payload": output, "task_output": output},
            tasks=[wait_task],
            workflow={"workflow_type": "deliver_work_order"},
        )
        ctx.memory_client.get_workflow.return_value = {"workflow": ctx.workflow, "tasks": ctx.tasks}
        result = wait_external.resume(ctx)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("dependency-audit", result["reason"])
        ctx.memory_client.resume_external.assert_not_called()
        ctx.memory_client.block_task_api.assert_called_once()

    def test_wait_external_resume_uses_dedicated_resume_endpoint(self):
        skeleton = load_skeleton("prd_request")
        wait_task = {
            "id": "wait-1",
            "step_name": "await_intake_answers",
            "status": "blocked",
            "error": "awaiting_external:channel.intake_response",
            "expected_event_type": "channel.intake_response",
        }
        ctx = self._ctx(
            task=None,
            stage=None,
            skeleton=skeleton,
            extras={
                "event_type": "channel.intake_response",
                "payload": {"user_text": "looks good"},
                "task_output": {
                    "clarified_requirements": {"answers": "looks good"},
                },
            },
            tasks=[wait_task],
            workflow={"workflow_type": "prd_request"},
        )
        ctx.memory_client.get_workflow.return_value = {
            "workflow": ctx.workflow,
            "tasks": ctx.tasks,
        }
        ctx.memory_client.resume_external.return_value = {
            "status": "resumed",
            "task_id": "wait-1",
        }

        result = wait_external.resume(ctx)

        self.assertEqual(result["status"], "resumed")
        ctx.memory_client.resume_external.assert_called_once_with(
            "wf-1",
            event_type="channel.intake_response",
            payload={"user_text": "looks good"},
            actor="orchestrator",
            task_output={"clarified_requirements": {"answers": "looks good"}},
        )
        ctx.memory_client.complete_task_api.assert_not_called()
        ctx.memory_client.clear_task_wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
