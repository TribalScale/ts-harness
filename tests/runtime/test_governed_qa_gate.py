"""Phase 2 governed qa_gate behavior: contract, outcome, and match_all merge wait."""

from __future__ import annotations

import unittest
from typing import Any

from ts_harness.workflows import stage_contracts as sc
from ts_harness.workflows.hooks import wait_external
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


PR1 = "https://github.com/org/repo/pull/1"
PR2 = "https://github.com/org/repo/pull/2"


def _qa_contract(workflow_type: str = "deliver_work_order") -> dict[str, Any]:
    skeleton = load_skeleton(workflow_type)
    contract = skeleton.resolve_stage_contract("qa_gate")
    assert contract is not None
    return contract


def _qa_output(status: str, prs: list[dict[str, Any]], summary: str = "ok") -> dict[str, Any]:
    return {
        "status": status,
        "result": {"summary": summary, "prs": prs},
        "confidence": 0.9,
        "warnings": [],
        "follow_up_needed": False,
    }


def _pr(pr_url: str, status: str = "success", ref: str = "WO-001") -> dict[str, Any]:
    return {
        "pr_url": pr_url,
        "work_item_ref": ref,
        "status": status,
        "acceptance_criteria": [{"id": "AC1", "result": "pass"}],
        "tests": [{"name": "pytest", "result": "pass"}],
        "defects": [],
        "summary": "ready",
    }


class QAContractTests(unittest.TestCase):
    def test_valid_success_passes_and_unlocks(self):
        contract = _qa_contract()
        output = _qa_output("success", [_pr(PR1)])
        violations = sc.validate_stage_output(contract, output, {"pr_url": PR1}, [])
        self.assertEqual(violations, [])
        self.assertEqual(sc.resolve_stage_outcome(contract, output), "succeeded")

    def test_valid_failed_blocks(self):
        contract = _qa_contract()
        output = _qa_output("failed", [_pr(PR1, status="failed")])
        violations = sc.validate_stage_output(contract, output, {"pr_url": PR1}, [])
        self.assertEqual(violations, [])
        self.assertEqual(sc.resolve_stage_outcome(contract, output), "blocked")

    def test_pr_url_mismatch_blocks_with_violation(self):
        contract = _qa_contract()
        output = _qa_output("success", [_pr(PR2)])
        violations = sc.validate_stage_output(contract, output, {"pr_url": PR1}, [])
        self.assertTrue(violations)
        self.assertTrue(any("PR URL set must equal" in v.message for v in violations))

    def test_schema_invalid_output_blocks(self):
        contract = _qa_contract()
        # Missing result.prs entirely -> structural (repairable) violation.
        violations = sc.validate_stage_output(
            contract, {"status": "success", "result": {}}, {"pr_url": PR1}, []
        )
        self.assertTrue(violations)

    def test_success_with_failed_child_is_invalid(self):
        contract = _qa_contract()
        output = _qa_output("success", [_pr(PR1, status="failed")])
        violations = sc.validate_stage_output(contract, output, {"pr_url": PR1}, [])
        self.assertTrue(any("only be 'success'" in v.message for v in violations))

    def test_pr_with_failed_ci_cannot_pass(self):
        contract = _qa_contract("implement_delivery")
        stage_input = {"pr_urls": [PR1, PR2], "checks_state": "failure", "failed_checks": [f"{PR1}:security"]}
        passed = _qa_output("failed", [_pr(PR1), _pr(PR2, status="failed")])
        violations = sc.validate_stage_output(contract, passed, stage_input, [])
        self.assertTrue(any(PR1 in v.message and "failed CI" in v.message for v in violations))
        honest = _qa_output("failed", [_pr(PR1, status="failed"), _pr(PR2)])
        self.assertEqual(sc.validate_stage_output(contract, honest, stage_input, []), [])

    def test_failed_ci_without_pr_detail_fails_every_pr(self):
        contract = _qa_contract()
        output = _qa_output("success", [_pr(PR1)])
        violations = sc.validate_stage_output(contract, output, {"pr_url": PR1, "checks_state": "failure"}, [])
        self.assertTrue(any("failed CI" in v.message for v in violations))

    def test_every_work_order_acceptance_test_needs_a_criterion(self):
        contract = _qa_contract()
        body = (
            "# WO-102\n\n## E2E Acceptance Tests\n"
            "- Given an order with exactly 100 units, Then a 10% discount applies.\n"
            "- Given an order with exactly 50 units, Then a 5% discount applies.\n\n"
            "## Files\n- app/pricing.py\n"
        )
        generic = _pr(PR1)
        generic["acceptance_criteria"] = [{"criterion": "All tests pass", "status": "pass"}]
        violations = sc.validate_stage_output(contract, _qa_output("success", [generic]), {"pr_url": PR1, "work_order_body": body}, [])
        self.assertTrue(any("exactly 100 units" in v.message and "exactly 50 units" in v.message for v in violations))
        covered = _pr(PR1)
        covered["acceptance_criteria"] = [
            {"criterion": "Given an order with exactly 100 units, Then a 10% discount applies.", "status": "fail"},
            {"criterion": "exactly 50 units gets a 5% discount", "status": "pass"},
        ]
        self.assertEqual(sc.validate_stage_output(contract, _qa_output("failed", [{**covered, "status": "failed"}]), {"pr_url": PR1, "work_order_body": body}, []), [])

    def test_jira_multi_pr_set_equality(self):
        contract = _qa_contract("implement_delivery")
        output = _qa_output(
            "success", [_pr(PR1, ref="TFS-1"), _pr(PR2, ref="TFS-2")]
        )
        violations = sc.validate_stage_output(
            contract, output, {"pr_urls": [PR1, PR2]}, []
        )
        self.assertEqual(violations, [])
        # Missing one PR from the aggregate set is a violation.
        missing = sc.validate_stage_output(
            contract, output, {"pr_urls": [PR1, PR2, "https://github.com/org/repo/pull/3"]}, []
        )
        self.assertTrue(missing)


class _FakeMergeMemory:
    def __init__(self, workflow: dict[str, Any], tasks: list[dict[str, Any]]):
        self._workflow = workflow
        self._tasks = tasks
        self.waits: list[tuple[str, str]] = []
        self.blocked: list[tuple[str, str]] = []
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.resumed: list[dict[str, Any]] = []

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return {"workflow": self._workflow, "tasks": self._tasks}

    def set_task_wait(self, task_id, *, wait_token, expected_event_type, actor="workflow-runner", **deadlines):
        self.waits.append((task_id, expected_event_type))

    def block_task_api(self, task_id, error, output=None, *, actor="workflow-runner"):
        self.blocked.append((task_id, error))

    def patch_task_input(self, task_id, input_data, *, actor="workflow-runner"):
        self.patched.append((task_id, dict(input_data)))
        # reflect the patch into the in-memory task so re-reads see received set
        for task in self._tasks:
            if str(task.get("id")) == str(task_id):
                task["input"] = dict(input_data)
        return {"id": task_id}

    def resume_external(self, workflow_id, *, event_type, payload, actor, task_output=None):
        self.resumed.append(
            {"event_type": event_type, "payload": payload, "task_output": task_output}
        )
        return {"status": "resumed", "task_id": "await-1"}


def _merge_ctx(memory, skeleton, *, extras=None, tasks=None, workflow=None):
    return HookContext(
        workflow_id="wf-1",
        workflow_type=skeleton.workflow_type,
        tenant_id="default",
        task=None,
        stage=None,
        skeleton=skeleton,
        actor="orchestrator",
        memory_client=memory,
        extras=extras or {},
        tasks=tasks or [],
        workflow=workflow or {},
    )


class QAMatchAllWaitTests(unittest.TestCase):
    def _wo_tasks(self, prs, *, merge_status="blocked", received=None):
        return [
            {
                "id": "qa-1",
                "step_name": "qa_gate",
                "status": "succeeded",
                "output": _qa_output("success", prs),
                "created_at": "2026-01-01",
            },
            {
                "id": "await-1",
                "step_name": "await_human_merge",
                "status": merge_status,
                "error": "awaiting_external:github.merge",
                "expected_event_type": "github.merge",
                "input": {"_merge_received": list(received or [])},
                "created_at": "2026-01-02",
            },
        ]

    def test_resolve_match_all_urls_from_qa_output(self):
        skeleton = load_skeleton("deliver_work_order")
        stage = skeleton.stage("await_human_merge")
        tasks = self._wo_tasks([_pr(PR1)])
        urls = wait_external.resolve_match_all_urls(
            stage.wait_external["match_all"], tasks=tasks, workflow={}
        )
        self.assertEqual(urls, [PR1])

    def test_qa_success_arms_wait(self):
        skeleton = load_skeleton("deliver_work_order")
        stage = skeleton.stage("await_human_merge")
        tasks = self._wo_tasks([_pr(PR1)], merge_status="pending")
        memory = _FakeMergeMemory({}, tasks)
        ctx = _merge_ctx(memory, skeleton)
        ctx.task = {"id": "await-1", "step_name": "await_human_merge", "input": {}}
        ctx.stage = stage
        result = wait_external.handle(ctx)
        self.assertEqual(result["status"], "awaiting_external")
        self.assertEqual(result["expected_pr_urls"], [PR1])
        self.assertEqual(memory.waits[0], ("await-1", "github.merge"))
        self.assertTrue(memory.blocked)

    def test_single_pr_merge_completes_wait(self):
        skeleton = load_skeleton("deliver_work_order")
        tasks = self._wo_tasks([_pr(PR1)])
        memory = _FakeMergeMemory({}, tasks)
        ctx = _merge_ctx(
            memory,
            skeleton,
            extras={
                "event_type": "github.merge",
                "payload": {"pr_url": PR1, "merge_sha": "abc"},
            },
        )
        result = wait_external.resume(ctx)
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(len(memory.resumed), 1)
        self.assertEqual(memory.resumed[0]["task_output"]["pr_urls"], [PR1])

    def test_pr_not_in_set_does_not_resume(self):
        skeleton = load_skeleton("deliver_work_order")
        tasks = self._wo_tasks([_pr(PR1)])
        memory = _FakeMergeMemory({}, tasks)
        ctx = _merge_ctx(
            memory,
            skeleton,
            extras={
                "event_type": "github.merge",
                "payload": {"pr_url": "https://github.com/org/repo/pull/999"},
            },
        )
        with self.assertRaises(ValueError):
            wait_external.resume(ctx)
        self.assertEqual(memory.resumed, [])

    def test_multi_pr_partial_then_complete(self):
        skeleton = load_skeleton("implement_delivery")
        tasks = self._wo_tasks([_pr(PR1, ref="T1"), _pr(PR2, ref="T2")])
        memory = _FakeMergeMemory({}, tasks)

        ctx1 = _merge_ctx(
            memory,
            skeleton,
            extras={"event_type": "github.merge", "payload": {"pr_url": PR1}},
        )
        first = wait_external.resume(ctx1)
        self.assertEqual(first["status"], "awaiting_more_merges")
        self.assertEqual(first["received"], [PR1])
        self.assertEqual(first["remaining"], [PR2])
        self.assertEqual(memory.resumed, [])
        # received persisted idempotently on the wait task input
        self.assertEqual(memory.patched[-1][1]["_merge_received"], [PR1])

        # Duplicate delivery of the same PR is idempotent (still awaiting PR2).
        ctx_dup = _merge_ctx(
            memory,
            skeleton,
            extras={"event_type": "github.merge", "payload": {"pr_url": PR1}},
        )
        dup = wait_external.resume(ctx_dup)
        self.assertEqual(dup["status"], "awaiting_more_merges")
        self.assertEqual(memory.resumed, [])

        ctx2 = _merge_ctx(
            memory,
            skeleton,
            extras={"event_type": "github.merge", "payload": {"pr_url": PR2}},
        )
        second = wait_external.resume(ctx2)
        self.assertEqual(second["status"], "resumed")
        self.assertEqual(len(memory.resumed), 1)
        self.assertEqual(memory.resumed[0]["task_output"]["pr_urls"], [PR1, PR2])

    def test_no_wait_armed_when_qa_blocked(self):
        # deliver + QA fail leaves no external wait: qa_gate blocked, merge pending.
        skeleton = load_skeleton("deliver_work_order")
        tasks = [
            {
                "id": "qa-1",
                "step_name": "qa_gate",
                "status": "blocked",
                "error": "stage contract outcome: blocked",
                "output": _qa_output("failed", [_pr(PR1, status="failed")]),
                "created_at": "2026-01-01",
            },
            {
                "id": "await-1",
                "step_name": "await_human_merge",
                "status": "pending",
                "created_at": "2026-01-02",
            },
        ]
        memory = _FakeMergeMemory({}, tasks)
        ctx = _merge_ctx(
            memory,
            skeleton,
            extras={"event_type": "github.merge", "payload": {"pr_url": PR1}},
        )
        # No task is awaiting_external, so a merge event has nothing to resume.
        with self.assertRaises(ValueError):
            wait_external.resume(ctx)
        self.assertEqual(memory.resumed, [])


if __name__ == "__main__":
    unittest.main()
