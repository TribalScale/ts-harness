"""Parallel work-order waves: selection, fork, child state, join, and reentry by wave."""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from ts_harness.config.runtime_settings import _canonicalize_program_defaults
from ts_harness.implementation.work_order_context import (
    max_parallel_work_orders,
    select_ready_work_order_ids,
)
from ts_harness.workflows import work_order_waves
from ts_harness.workflows.program_adapters import build_default_adapter_registry

IDS = ["WO-1", "WO-2", "WO-3", "WO-4", "WO-5"]
DEPS = {"WO-3": ["WO-1"], "WO-4": ["WO-1", "WO-2"], "WO-5": ["WO-3"]}


class SelectionTests(unittest.TestCase):
    def test_first_wave_holds_only_independent_work_orders(self):
        self.assertEqual(select_ready_work_order_ids(IDS, [], DEPS, max_parallel=5), ["WO-1", "WO-2"])

    def test_dependency_merges_unlock_the_next_wave(self):
        self.assertEqual(select_ready_work_order_ids(IDS, ["WO-1"], DEPS, max_parallel=5), ["WO-2", "WO-3"])
        self.assertEqual(select_ready_work_order_ids(IDS, ["WO-1", "WO-2"], DEPS, max_parallel=5), ["WO-3", "WO-4"])

    def test_cap_limits_the_wave_in_list_order(self):
        self.assertEqual(select_ready_work_order_ids(IDS, [], {}, max_parallel=3), ["WO-1", "WO-2", "WO-3"])
        self.assertEqual(select_ready_work_order_ids(IDS, [], {}, max_parallel=1), ["WO-1"])

    def test_nothing_ready_when_all_done_or_blocked(self):
        self.assertEqual(select_ready_work_order_ids(IDS, IDS, DEPS), [])
        self.assertEqual(select_ready_work_order_ids(["WO-5"], [], DEPS), [])

    def test_max_parallel_setting(self):
        self.assertEqual(max_parallel_work_orders({}), 3)
        self.assertEqual(max_parallel_work_orders({"delivery": {"max_parallel_work_orders": 5}}), 5)
        issues: list[dict[str, str]] = []
        out = _canonicalize_program_defaults({"delivery": {"max_parallel_work_orders": 4}}, path="$", issues=issues)
        self.assertEqual((out["delivery"], issues), ({"max_parallel_work_orders": 4}, []))
        _canonicalize_program_defaults({"delivery": {"max_parallel_work_orders": 0}}, path="$", issues=issues)
        self.assertTrue(any(issue["code"] == "invalid_max_parallel" for issue in issues))


class FakeMemory:
    def __init__(self, workflows: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] | None = None):
        self.workflows = workflows or {}
        self.created: list[dict[str, Any]] = []
        self.tasks_created: list[dict[str, Any]] = []
        self.patched: dict[str, dict[str, Any]] = {}
        self.blocked: list[tuple[str, str]] = []
        self.resumed: list[dict[str, Any]] = []

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow, tasks = self.workflows.get(workflow_id, ({"id": workflow_id}, []))
        return {"workflow": workflow, "tasks": tasks}

    def create_workflow(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {"id": kwargs["workflow_id"]}

    def create_task(self, body: dict[str, Any]) -> dict[str, Any]:
        self.tasks_created.append(body)
        return {"id": f"task-{len(self.tasks_created)}"}

    def patch_task_input(self, task_id: str, input_data: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
        self.patched[task_id] = input_data
        return {}

    def block_task_api(self, task_id: str, error: str, output: dict | None = None, *, actor: str = "") -> dict[str, Any]:
        self.blocked.append((task_id, error))
        return {}

    def resume_external(self, workflow_id: str, **kwargs: Any) -> dict[str, Any]:
        self.resumed.append({"workflow_id": workflow_id, **kwargs})
        return {"status": "resumed", "task_id": "t-join"}


def _child(workflow_id: str, *, merged: bool = True, failed: bool = False, sha: str = "", completed_at: str = "") -> tuple[dict, list]:
    tasks = [
        {"step_name": "deliver_work_order", "status": "cancelled", "output": {}},
        {"step_name": "record_implement_failure", "status": "ready", "output": {}},
        {"step_name": "deliver_work_order", "status": "failed" if failed else "succeeded", "output": {"pr_url": f"https://github.com/acme/shop/pull/{workflow_id[-1]}"}},
        {"step_name": "await_pr_checks", "status": "succeeded", "output": {"checks_state": "success"}},
        {"step_name": "qa_gate", "status": "succeeded", "output": {"status": "success"}},
        {
            "step_name": "await_human_merge",
            "status": "succeeded" if merged else "blocked",
            "output": {"merge_sha": sha} if merged else {},
            "completed_at": completed_at,
        },
    ]
    return {"id": workflow_id, "workflow_type": "deliver_work_order", "parent_workflow_id": "wf-parent"}, tasks


class ForkWaveTests(unittest.TestCase):
    def test_fork_creates_one_child_per_work_order_with_shared_context(self):
        task = {
            "id": "t-wave",
            "workflow_id": "wf-parent",
            "tenant_id": "acme",
            "input": {
                "work_orders": [{"work_order_id": "WO-1"}, {"work_order_id": "WO-2"}],
                "repo_url": "https://github.com/acme/shop",
                "base_branch": "dev",
                "work_orders_markdown": "## WO-1 Login\nbody one\n## WO-2 Cart\nbody two\n",
            },
        }
        memory = FakeMemory()
        output = work_order_waves.fork_wave(task, {"workflow_type": "deliver_work_order"}, memory=memory)
        self.assertEqual(output["wave_work_order_ids"], ["WO-1", "WO-2"])
        self.assertEqual(len(output["child_workflow_ids"]), 2)
        self.assertTrue(all(call["parent_workflow_id"] == "wf-parent" for call in memory.created))
        intakes = [body["input"] for body in memory.tasks_created if body["step_name"] == "deliver_work_order"]
        self.assertEqual([intake["work_order_id"] for intake in intakes], ["WO-1", "WO-2"])
        self.assertEqual(intakes[1]["base_branch"], "dev")
        self.assertIn("body two", intakes[1]["work_order_body"])
        qa = next(body for body in memory.tasks_created if body["step_name"] == "qa_gate")
        self.assertTrue(qa["depends_on"])

    def test_legacy_single_work_order_input_still_forks(self):
        memory = FakeMemory()
        task = {"id": "t", "workflow_id": "wf-parent", "input": {"work_order_id": "WO-7"}}
        output = work_order_waves.fork_wave(task, {}, memory=memory)
        self.assertEqual(output["wave_work_order_ids"], ["WO-7"])

    def test_empty_wave_blocks(self):
        from ts_harness.workflows.system_steps import run_system_step

        result = run_system_step("fork_work_order_wave", {"id": "t", "workflow_id": "wf"}, {}, memory=FakeMemory())
        self.assertEqual(result["status"], "blocked")


class ChildStateTests(unittest.TestCase):
    def test_compensate_and_superseded_tasks_are_ignored(self):
        workflow, tasks = _child("wf-c1", sha="s1")
        self.assertEqual(work_order_waves.child_state(workflow, tasks), "succeeded")

    def test_running_child_is_not_terminal(self):
        workflow, tasks = _child("wf-c1", merged=False)
        self.assertIsNone(work_order_waves.child_state(workflow, tasks))

    def test_failed_child(self):
        workflow, tasks = _child("wf-c1", failed=True)
        self.assertEqual(work_order_waves.child_state(workflow, tasks), "failed")

    def test_terminal_block_counts_as_failed(self):
        workflow, tasks = _child("wf-c1", merged=False)
        for task in tasks:
            if task["step_name"] == "deliver_work_order" and task["status"] == "succeeded":
                task["status"] = "blocked"
                task["error"] = "git clone failed: repository not found"
        self.assertEqual(work_order_waves.child_state(workflow, tasks), "failed")

    def test_block_waiting_for_a_human_decision_keeps_the_child_running(self):
        workflow, tasks = _child("wf-c1", merged=False)
        for task in tasks:
            if task["step_name"] == "deliver_work_order" and task["status"] == "succeeded":
                task["status"] = "blocked"
                task["error"] = "barrier_blocked: code_review_blocked: 1 blocking findings"
                task["input"] = {**(task.get("input") or {}), "_awaiting_human_decision": {"reason": "revision_limit"}}
        self.assertIsNone(work_order_waves.child_state(workflow, tasks))

    def test_closed_pr_fails_the_child(self):
        workflow, tasks = _child("wf-c1", merged=False)
        for task in tasks:
            if task["step_name"] == "await_human_merge":
                task["error"] = "external_blocked:github.pr_closed by bob"
        self.assertEqual(work_order_waves.child_state(workflow, tasks), "failed")

    def test_external_wait_stays_pending(self):
        workflow, tasks = _child("wf-c1", merged=False)
        for task in tasks:
            if task["step_name"] == "await_human_merge":
                task["error"] = "awaiting_external:github.merge"
        self.assertIsNone(work_order_waves.child_state(workflow, tasks))


CHILDREN = [{"workflow_id": "wf-c1", "work_order_id": "WO-1"}, {"workflow_id": "wf-c2", "work_order_id": "WO-2"}]


def _join_ctx(memory: FakeMemory):
    from ts_harness.workflows.materialize import load_skeleton
    from ts_harness.workflows.registry import HookContext

    memory.workflows["wf-parent"] = (
        {"id": "wf-parent"},
        [
            {
                "id": "t-join",
                "step_name": "join_wave",
                "status": "blocked",
                "error": "awaiting_external:workflow.child_terminal",
                "expected_event_type": "workflow.child_terminal",
                "input": {"_wait_options": {"children": CHILDREN}},
            }
        ],
    )
    skeleton = load_skeleton("implement_work_orders")
    return HookContext(
        workflow_id="wf-parent",
        workflow_type="implement_work_orders",
        tenant_id="acme",
        task=None,
        stage=None,
        skeleton=skeleton,
        actor="workflow-runner",
        memory_client=memory,
        extras={"event_type": "workflow.child_terminal", "payload": {"child_workflow_id": "wf-c2", "state": "succeeded"}},
    )


class JoinTests(unittest.TestCase):
    def test_join_waits_until_every_child_finished(self):
        from ts_harness.workflows.hooks import wait_external

        memory = FakeMemory({"wf-c1": _child("wf-c1", sha="s1"), "wf-c2": _child("wf-c2", merged=False)})
        result = wait_external.resume(_join_ctx(memory))
        self.assertEqual(result["status"], "awaiting_more_children")
        self.assertEqual(result["pending"], ["WO-2"])
        self.assertEqual(memory.resumed, [])

    def test_join_succeeds_with_latest_merge_sha_and_all_prs(self):
        from ts_harness.workflows.hooks import wait_external

        memory = FakeMemory(
            {
                "wf-c1": _child("wf-c1", sha="s1", completed_at="2026-09-30T10:00:00"),
                "wf-c2": _child("wf-c2", sha="s2", completed_at="2026-09-30T11:00:00"),
            }
        )
        wait_external.resume(_join_ctx(memory))
        output = memory.resumed[0]["task_output"]
        self.assertEqual(output["wave_work_order_ids"], ["WO-1", "WO-2"])
        self.assertEqual(output["merge_sha"], "s2")
        self.assertEqual(len(output["pr_urls"]), 2)

    def test_terminal_block_blocks_join_with_the_child_error(self):
        from ts_harness.workflows.hooks import wait_external

        workflow, tasks = _child("wf-c2", merged=False)
        for task in tasks:
            if task["step_name"] == "deliver_work_order" and task["status"] == "succeeded":
                task["status"] = "blocked"
                task["error"] = "git clone failed: repository not found"
        memory = FakeMemory({"wf-c1": _child("wf-c1", sha="s1"), "wf-c2": (workflow, tasks)})
        result = wait_external.resume(_join_ctx(memory))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("repository not found", memory.blocked[0][1])
        self.assertIn("WO-2", memory.blocked[0][1])

    def test_partial_failure_blocks_join_and_keeps_merged_results(self):
        from ts_harness.workflows.hooks import wait_external

        memory = FakeMemory({"wf-c1": _child("wf-c1", sha="s1"), "wf-c2": _child("wf-c2", failed=True)})
        result = wait_external.resume(_join_ctx(memory))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("WO-2", memory.blocked[0][1])
        self.assertEqual(memory.resumed, [])

    def test_prepare_join_is_ready_when_children_already_finished(self):
        memory = FakeMemory({"wf-c1": _child("wf-c1", sha="s1"), "wf-c2": _child("wf-c2", sha="s2")})
        memory.workflows["wf-parent"] = (
            {"id": "wf-parent"},
            [{"id": "t-wave", "step_name": "deliver_wave", "status": "succeeded", "output": {"children": CHILDREN}}],
        )
        prepared = work_order_waves.prepare_children_wait(
            {"id": "t-join", "workflow_id": "wf-parent"},
            {"children": {"source": "$prior.deliver_wave.output.children"}},
            memory=memory,
        )
        self.assertEqual(memory.patched["t-join"]["_wait_options"]["children"], CHILDREN)
        self.assertEqual(prepared["ready"], {"wave_state": "succeeded"})


class ChildNotificationTests(unittest.TestCase):
    def test_finished_child_notifies_parent_join(self):
        from ts_harness.workflows.hooks import complete_program_phase
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.registry import HookContext

        memory = FakeMemory({"wf-c1": _child("wf-c1", sha="s1")})
        ctx = HookContext(
            workflow_id="wf-c1",
            workflow_type="deliver_work_order",
            tenant_id="acme",
            task={"id": "t"},
            stage=None,
            skeleton=load_skeleton("deliver_work_order"),
            actor="workflow-runner",
            memory_client=memory,
        )
        with mock.patch.object(work_order_waves, "notify_parent", return_value={"status": "resumed"}) as notify:
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "child_finished")
        self.assertEqual(notify.call_args.args[2], "succeeded")


class ReentryTests(unittest.TestCase):
    def test_reentry_advances_the_next_wave(self):
        from ts_harness.workflows.hooks import complete_program_phase
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.registry import HookContext

        client = mock.MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-1", "program_id": "prog", "program_phase": "implement", "summary": "ship"},
            "tasks": [{"status": "succeeded", "output": {"wave_work_order_ids": ["WO-1", "WO-2"]}}],
        }
        client.get_program.return_value = {
            "program": {
                "id": "prog",
                "metadata": {
                    "artifacts": {
                        "work_order_ids": IDS,
                        "completed_work_order_ids": ["WO-1", "WO-2"],
                        "work_order_dependencies": DEPS,
                    }
                },
            }
        }
        service = mock.MagicMock()
        service.advance_program.return_value = {"workflow_id": "wf-2"}
        skeleton = load_skeleton("implement_work_orders")
        ctx = HookContext(
            workflow_id="wf-1",
            workflow_type="implement_work_orders",
            tenant_id="acme",
            task={"id": "t"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )
        with mock.patch.object(complete_program_phase, "get_delivery_program_service", return_value=service), mock.patch(
            "ts_harness.workflows.program_adapters._live_program_defaults",
            return_value={"delivery": {"max_parallel_work_orders": 2}},
        ), mock.patch.object(complete_program_phase, "publish_program_brief"):
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "phase_reentered_implement")
        artifacts = service.advance_program.call_args.kwargs["artifacts"]
        self.assertEqual(artifacts["next_work_order_ids"], ["WO-3", "WO-4"])
        self.assertEqual(artifacts["next_work_order_id"], "WO-3")


class AdapterTests(unittest.TestCase):
    def test_wave_outputs_complete_every_work_order(self):
        patch = build_default_adapter_registry().extract_outputs(
            "extract_implement_outputs",
            workflow_type="implement_work_orders",
            tasks=[{"status": "succeeded", "output": {"wave_work_order_ids": ["WO-2", "WO-3"], "pr_urls": ["https://p/2"]}}],
            artifacts={"completed_work_order_ids": ["WO-1"], "pr_urls": ["https://p/1"]},
        )
        self.assertEqual(patch["completed_work_order_ids"], ["WO-1", "WO-2", "WO-3"])
        self.assertEqual(patch["pr_urls"], ["https://p/1", "https://p/2"])

    def test_implement_inputs_carry_the_wave(self):
        with mock.patch(
            "ts_harness.workflows.program_adapters._live_program_defaults",
            return_value={"delivery": {"max_parallel_work_orders": 3}},
        ):
            intake = build_default_adapter_registry().build_inputs(
                "extract_implement_outputs",
                summary="ship",
                artifacts={"work_order_ids": IDS, "completed_work_order_ids": [], "work_order_dependencies": DEPS, "next_work_order_id": "WO-1"},
            )
        self.assertEqual([item["work_order_id"] for item in intake["work_orders"]], ["WO-1", "WO-2"])
        self.assertEqual(intake["work_order_id"], "WO-1")


if __name__ == "__main__":
    unittest.main()
