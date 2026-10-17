"""fork_children system step, child version pinning, generic join outputs and fork validation."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ts_harness.workflows import child_workflows, work_order_waves
from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.hooks import fork_subworkflow
from ts_harness.workflows.materialize import (
    build_fork_graph,
    build_workflow_catalog,
    load_skeleton,
    materialize_skeleton_tasks,
)
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.system_steps import run_system_step

from tests.runtime.test_work_order_waves import FakeMemory

CHILD = "deliver_work_order"
CATALOGS = {
    "hook_names": {"hydrate_input", "revise_on_reject", "compensate_on_failure", "cancel_workflow"},
    "agent_ids": {"orchestrator"},
    "skill_ids": set(),
    "built_in_types": set(),
}
LOCALES = [{"code": "en", "title": "English"}, {"code": "fr", "title": "French"}]


def _parent(input_data: dict | None = None) -> tuple[dict, FakeMemory]:
    tasks = [
        {"id": "t-plan", "step_name": "plan", "status": "succeeded", "output": {"locales": LOCALES}},
        {"id": "t-fork", "step_name": "fork_locales", "status": "running", "input": dict(input_data or {})},
    ]
    memory = FakeMemory({"wf-parent": ({"id": "wf-parent"}, tasks)})
    task = {"id": "t-fork", "workflow_id": "wf-parent", "tenant_id": "acme", "input": dict(input_data or {})}
    return task, memory


FORK_CONFIG = {
    "hook": "fork_children",
    "workflow_type": CHILD,
    "for_each": "$prior.plan.output.locales",
    "label": "$item.code",
    "input_map": {"work_order_id": "$item.code", "work_order_body": "$item.title"},
}


def _intakes(memory: FakeMemory) -> list[dict]:
    return [body["input"] for body in memory.tasks_created if body["step_name"] == "deliver_work_order"]


class ForkChildrenTests(unittest.TestCase):
    def test_one_child_per_item_with_mapped_intake_and_labels(self):
        task, memory = _parent()
        output = child_workflows.fork_children(task, FORK_CONFIG, memory=memory)
        self.assertEqual([child["label"] for child in output["children"]], ["en", "fr"])
        self.assertEqual(output["child_workflow_ids"], [child["workflow_id"] for child in output["children"]])
        self.assertEqual(output["child_workflow_type"], CHILD)
        self.assertEqual([call["workflow_type"] for call in memory.created], [CHILD, CHILD])
        self.assertTrue(all(call["parent_workflow_id"] == "wf-parent" for call in memory.created))
        self.assertEqual(
            [(intake["work_order_id"], intake["work_order_body"]) for intake in _intakes(memory)],
            [("en", "English"), ("fr", "French")],
        )
        self.assertEqual(memory.patched["t-fork"]["_forked_children"], output["children"])

    def test_rerun_skips_children_already_forked(self):
        task, memory = _parent({"_forked_children": [{"workflow_id": "wf-en", "label": "en"}]})
        output = child_workflows.fork_children(task, FORK_CONFIG, memory=memory)
        self.assertEqual(len(memory.created), 1)
        self.assertEqual(output["children"][0], {"workflow_id": "wf-en", "label": "en"})
        self.assertEqual(output["children"][1]["label"], "fr")

    def test_single_child_without_for_each_uses_index_label(self):
        task, memory = _parent()
        config = {"hook": "fork_children", "workflow_type": CHILD, "input_map": {"work_order_id": "WO-9"}}
        output = child_workflows.fork_children(task, config, memory=memory)
        self.assertEqual([child["label"] for child in output["children"]], ["0"])
        self.assertEqual(_intakes(memory)[0]["work_order_id"], "WO-9")

    def test_bad_configs_block_the_barrier(self):
        task, memory = _parent()
        cases = [
            {"hook": "fork_children"},
            {**FORK_CONFIG, "for_each": "$prior.missing.output.items"},
            {**FORK_CONFIG, "label": "dup"},
        ]
        for config in cases:
            with self.subTest(config=config):
                result = run_system_step("fork_children", task, config, memory=memory)
                self.assertEqual(result["status"], "blocked")
        self.assertEqual(memory.created, [])


class VersionPinningTests(unittest.TestCase):
    def test_fork_stages_seed_the_current_child_version(self):
        parent = load_skeleton("implement_work_orders")
        bodies = materialize_skeleton_tasks(parent, workflow_id="wf-parent")
        wave = next(body for body in bodies if body["step_name"] == "deliver_wave")
        self.assertEqual(wave["input"]["_child_version"], load_skeleton(CHILD).definition_version)
        join = next(body for body in bodies if body["step_name"] == "join_wave")
        self.assertNotIn("_child_version", join["input"])

    def test_pinned_version_loads_through_the_engine(self):
        memory = FakeMemory()
        skeleton = load_skeleton(CHILD)
        engine = SimpleNamespace(skeleton=mock.Mock(return_value=skeleton))
        with mock.patch("ts_harness.workflows.engine.get_workflow_engine", return_value=engine):
            fork_subworkflow.fork_child(
                memory,
                parent_workflow_id="wf-parent",
                tenant_id="acme",
                child_type=CHILD,
                intake_input={"work_order_id": "WO-1"},
                actor="test",
                definition_version="0.9.0",
            )
        engine.skeleton.assert_called_once_with(CHILD, definition_version="0.9.0")

    def test_fork_children_passes_the_pinned_version(self):
        task, memory = _parent({"_child_version": "0.9.0"})
        with mock.patch.object(child_workflows, "fork_child", return_value="wf-c") as fork:
            child_workflows.fork_children(task, FORK_CONFIG, memory=memory)
        self.assertEqual({call.kwargs["definition_version"] for call in fork.call_args_list}, {"0.9.0"})


def _generic_child(workflow_id: str, *, failed: bool = False) -> tuple[dict, list]:
    tasks = [{"step_name": "translate", "status": "failed" if failed else "succeeded", "output": {"text": workflow_id}}]
    return {"id": workflow_id, "workflow_type": "translate_page", "parent_workflow_id": "wf-parent"}, tasks


class GenericJoinTests(unittest.TestCase):
    def test_children_outputs_by_label(self):
        memory = FakeMemory({"wf-en": _generic_child("wf-en"), "wf-fr": _generic_child("wf-fr")})
        children = [{"workflow_id": "wf-en", "label": "en"}, {"workflow_id": "wf-fr", "label": "fr"}]
        result = work_order_waves.wave_result(memory, children)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["output"]["children_outputs"], {"en": {"text": "wf-en"}, "fr": {"text": "wf-fr"}})
        self.assertEqual(result["output"]["failed_labels"], [])

    def test_failed_and_pending_children_are_reported_by_label(self):
        memory = FakeMemory({"wf-en": _generic_child("wf-en", failed=True)})
        result = work_order_waves.wave_result(memory, [{"workflow_id": "wf-en", "label": "en"}])
        self.assertEqual((result["state"], result["output"]["failed_labels"]), ("failed", ["en"]))
        running = FakeMemory({"wf-fr": ({"id": "wf-fr", "workflow_type": "translate_page"}, [{"step_name": "translate", "status": "running"}])})
        self.assertEqual(work_order_waves.wave_result(running, [{"workflow_id": "wf-fr", "label": "fr"}])["pending"], ["fr"])

    def test_join_keeps_labels_from_the_fork_output(self):
        tasks = [{"step_name": "fork_locales", "status": "succeeded", "output": {"children": [{"workflow_id": "wf-en", "label": "en"}]}}]
        children = work_order_waves._wave_children({"source": "$prior.fork_locales.output.children"}, tasks=tasks, workflow={})
        self.assertEqual(children, [{"workflow_id": "wf-en", "work_order_id": "", "label": "en"}])

    def test_fork_subworkflow_no_longer_blocks_its_own_task(self):
        memory = FakeMemory()
        stage = SimpleNamespace(on_complete={"hook": "fork_subworkflow", "workflow_type": CHILD, "join_stage": "join"})
        ctx = HookContext(
            workflow_id="wf-parent",
            workflow_type="legacy",
            tenant_id="acme",
            task={"id": "t-1", "status": "succeeded"},
            stage=stage,
            skeleton=None,
            actor="test",
            memory_client=memory,
        )
        result = fork_subworkflow.handle(ctx)
        self.assertEqual((result["status"], memory.blocked), ("forked", []))


def _fork_workflow(**overrides) -> dict:
    fork = {"name": "fork_locales", "kind": "task", "agent_id": "orchestrator", "depends_on": ["plan"], "barrier": dict(FORK_CONFIG)}
    join = {
        "name": "join_locales",
        "kind": "task",
        "agent_id": "orchestrator",
        "depends_on": ["fork_locales"],
        "wait_external": {
            "hook": "wait_external",
            "event_type": "workflow.child_terminal",
            "children": {"source": "$prior.fork_locales.output.children"},
        },
    }
    fork.update(overrides.pop("fork", {}))
    join.update(overrides.pop("join", {}))
    return {
        "workflow_type": overrides.pop("workflow_type", "localize_site"),
        "definition_version": "1.0.0",
        "stages": [{"name": "plan", "kind": "task", "agent_id": "orchestrator", "approval_required": True}, fork, join],
    }


def _codes(payload: dict, **kwargs) -> list[tuple[str, str]]:
    kwargs.setdefault("workflow_catalog", build_workflow_catalog())
    kwargs.setdefault("fork_graph", build_fork_graph())
    _skeleton, issues = validate_workflow_definition(payload, **CATALOGS, **kwargs)
    return [(issue.code, issue.severity) for issue in issues]


class ForkValidationTests(unittest.TestCase):
    def test_valid_fork_and_join(self):
        self.assertEqual([code for code in _codes(_fork_workflow()) if code[1] == "error"], [])

    def test_catalog_publishes_child_input_keys(self):
        self.assertIn("repo_url", build_workflow_catalog()[CHILD]["workflow_input_keys"])
        self.assertEqual(build_fork_graph()["implement_work_orders"], {CHILD})

    def test_unmapped_child_inputs_warn(self):
        self.assertIn(("child_input_not_mapped", "warning"), _codes(_fork_workflow()))

    def test_unknown_child_and_missing_type(self):
        self.assertIn(("unknown_child_workflow", "error"), _codes(_fork_workflow(fork={"barrier": {**FORK_CONFIG, "workflow_type": "ghost"}})))
        self.assertIn(("fork_missing_workflow_type", "error"), _codes(_fork_workflow(fork={"barrier": {"hook": "fork_children"}})))

    def test_cycle_through_the_fork_graph(self):
        graph = {CHILD: {"localize_site"}}
        self.assertIn(("child_workflow_cycle", "error"), _codes(_fork_workflow(), fork_graph=graph))
        self_fork = _fork_workflow(fork={"barrier": {**FORK_CONFIG, "workflow_type": "localize_site"}})
        self.assertIn(("child_workflow_cycle", "error"), _codes(self_fork, workflow_catalog=None))

    def test_join_must_read_and_depend_on_a_fork_stage(self):
        bad_source = _fork_workflow(join={"wait_external": {"hook": "wait_external", "event_type": "workflow.child_terminal", "children": {"source": "$prior.plan.output.children"}}})
        self.assertIn(("join_source_not_fork", "error"), _codes(bad_source))
        no_dep = _fork_workflow(join={"depends_on": ["plan"]})
        self.assertIn(("join_missing_dependency", "error"), _codes(no_dep))

    def test_fork_subworkflow_is_retired(self):
        payload = _fork_workflow()
        payload["stages"][0]["on_complete"] = {"hook": "fork_subworkflow", "workflow_type": CHILD}
        self.assertIn(("fork_subworkflow_retired", "error"), _codes(payload, workflow_catalog=None, fork_graph=None))


if __name__ == "__main__":
    unittest.main()
