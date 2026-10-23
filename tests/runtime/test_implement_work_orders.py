"""Tests for the implement_work_orders wave parent and the deliver_work_order child pipeline."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.materialize import load_skeleton, materialize_skeleton_tasks


class ImplementWorkOrdersSkeletonTests(unittest.TestCase):
    def test_skeleton_loads(self):
        skeleton = load_skeleton("implement_work_orders")
        self.assertEqual(skeleton.workflow_type, "implement_work_orders")
        self.assertEqual(skeleton.definition_version, "2.0.0")
        self.assertEqual(skeleton.program.get("phase"), "implement")
        self.assertTrue(skeleton.program.get("terminal"))
        self.assertFalse(skeleton.program.get("next_phase"))
        self.assertEqual(skeleton.intake_stage_name(), "deliver_wave")

    def test_wave_forks_children_and_joins(self):
        skeleton = load_skeleton("implement_work_orders")
        self.assertEqual([stage.name for stage in skeleton.stages], ["deliver_wave", "join_wave"])
        wave = skeleton.stage("deliver_wave")
        self.assertEqual(wave.barrier, {"hook": "fork_work_order_wave", "workflow_type": "deliver_work_order"})
        join = skeleton.stage("join_wave")
        self.assertEqual(join.depends_on, ["deliver_wave"])
        self.assertEqual(join.wait_external["event_type"], "workflow.child_terminal")
        self.assertEqual(join.wait_external["children"], {"source": "$prior.deliver_wave.output.children"})


REVIEW_CHAIN = [
    ("review_precheck", ["$re_gate.id"]),
    ("code_review", ["$previous.id"]),
    ("finalize_code_review", ["$previous.id"]),
    ("qa_gate", ["$previous.id"]),
    ("ensure_fresh", ["$previous.id"]),
    ("await_human_merge", ["$previous.id"]),
]


class DeliverWorkOrderSkeletonTests(unittest.TestCase):
    def test_child_skeleton_loads(self):
        skeleton = load_skeleton("deliver_work_order")
        self.assertEqual(skeleton.definition_version, "1.5.0")
        self.assertEqual(skeleton.intake_stage_name(), "deliver_work_order")
        self.assertFalse(skeleton.program)

    def test_stages_enforce_checks_review_then_qa_gate_before_merge(self):
        skeleton = load_skeleton("deliver_work_order")
        names = [stage.name for stage in skeleton.stages]
        self.assertEqual(
            names,
            [
                "deliver_work_order",
                "record_implement_failure",
                "await_pr_checks",
                "review_precheck",
                "code_review",
                "finalize_code_review",
                "qa_gate",
                "ensure_fresh",
                "await_human_merge",
            ],
        )
        checks = skeleton.stage("await_pr_checks")
        self.assertEqual(checks.depends_on, ["deliver_work_order"])
        self.assertEqual(checks.wait_external["event_type"], "github.pr_checks")
        self.assertEqual(
            checks.wait_external["pr_checks"],
            {
                "pr_url": "$prior.deliver_work_order.output.pr_url",
                "security": "$prior.deliver_work_order.output.security",
            },
        )
        self.assertEqual(
            checks.on_complete,
            {"hook": "hydrate_input", "downstream": ["review_precheck", "code_review", "qa_gate"]},
        )
        deliver = skeleton.stage("deliver_work_order")
        self.assertEqual(deliver.skill_hints, ["verified-merge-governed"])
        self.assertEqual(deliver.present_prompt, "none")
        compensate = skeleton.stage("record_implement_failure")
        self.assertEqual(compensate.kind, "compensate")

    def test_deliver_hydrates_qa_gate_on_complete(self):
        skeleton = load_skeleton("deliver_work_order")
        deliver = skeleton.stage("deliver_work_order")
        self.assertEqual(deliver.on_complete.get("hook"), "hydrate_input")
        self.assertEqual(deliver.on_complete.get("downstream"), ["qa_gate"])
        self.assertEqual(deliver.on_failure.get("hook"), "compensate_on_failure")
        self.assertEqual(
            deliver.on_failure.get("compensate_stage"), "record_implement_failure"
        )

    def test_qa_gate_contract_and_hydration(self):
        skeleton = load_skeleton("deliver_work_order")
        qa = skeleton.stage("qa_gate")
        self.assertEqual(qa.agent_id, "qa-agent")
        self.assertEqual(qa.depends_on, ["finalize_code_review"])
        self.assertEqual(qa.input_map["checks_state"], "$prior.await_pr_checks.output.checks_state")
        self.assertEqual(qa.input_map["files"], "$prior.review_precheck.output.files")
        self.assertEqual(qa.skill_hints, ["governed-qa-gate"])
        contract = skeleton.resolve_stage_contract("qa_gate")
        self.assertIsNotNone(contract)
        self.assertEqual(contract["outcome"]["blocked_values"], ["failed"])
        self.assertIn("validate_qa_pr_url_set", contract["semantic_validators"])
        # qa_gate pulls the PR + program context it needs to evaluate.
        self.assertEqual(
            qa.input_map["pr_url"], "$prior.deliver_work_order.output.pr_url"
        )
        self.assertEqual(
            qa.input_map["work_order_ref"],
            "$prior.deliver_work_order.output.work_order_id",
        )
        self.assertEqual(qa.input_map["repo_url"], "$workflow.repo_url")
        self.assertEqual(qa.present_prompt, "merge")

    def test_qa_gate_on_block_revises_deliver_and_rearms_merge(self):
        skeleton = load_skeleton("deliver_work_order")
        qa = skeleton.stage("qa_gate")
        on_block = qa.on_block or {}
        self.assertEqual(on_block.get("hook"), "revise_on_reject")
        self.assertEqual(on_block.get("max_revisions"), 3)
        revise = on_block.get("revise") or {}
        self.assertEqual(revise.get("step_name"), "deliver_work_order")
        self.assertEqual(revise.get("agent_id"), "dev-agent")
        self.assertEqual(revise.get("source_steps"), ["deliver_work_order"])
        self.assertEqual(revise.get("input_map", {}).get("qa_feedback"), "$reject.comments")
        self.assertEqual(
            revise.get("input_map", {}).get("pr_url"),
            "$prior.deliver_work_order.output.pr_url",
        )
        re_gate = on_block.get("re_gate") or {}
        self.assertEqual(re_gate.get("step_name"), "await_pr_checks")
        self.assertEqual(re_gate.get("approval_required"), False)
        resume = on_block.get("resume_after") or []
        self.assertEqual(
            [(step["step_name"], step["depends_on"]) for step in resume],
            REVIEW_CHAIN,
        )

    def test_await_human_merge_depends_on_ensure_fresh_with_match_all(self):
        skeleton = load_skeleton("deliver_work_order")
        merge = skeleton.stage("await_human_merge")
        self.assertEqual(merge.depends_on, ["ensure_fresh"])
        match_all = merge.wait_external.get("match_all")
        self.assertIsNotNone(match_all)
        self.assertEqual(match_all["source"], "$prior.qa_gate.output.result.prs")
        self.assertEqual(match_all["item_key"], "pr_url")
        # No exact single-PR match rule remains.
        self.assertNotIn("match", merge.wait_external)

    def test_ensure_fresh_and_merge_refresh_loop(self):
        skeleton = load_skeleton("deliver_work_order")
        fresh = skeleton.stage("ensure_fresh")
        self.assertEqual(fresh.depends_on, ["qa_gate"])
        self.assertEqual(fresh.barrier, {"hook": "ensure_fresh", "pr_urls": "$prior.qa_gate.output.result.prs"})
        for stage in (fresh, skeleton.stage("await_human_merge")):
            on_block = stage.on_block or {}
            self.assertEqual(on_block.get("max_revisions"), 5)
            revise = on_block["revise"]
            self.assertEqual((revise["step_name"], revise["budget"]), ("deliver_work_order", "refresh"))
            self.assertEqual(revise["static_input"], {"refresh": True})
            self.assertEqual(
                [(step["step_name"], step["depends_on"]) for step in on_block["resume_after"]],
                REVIEW_CHAIN,
            )

    def test_code_review_stages(self):
        skeleton = load_skeleton("deliver_work_order")
        precheck = skeleton.stage("review_precheck")
        self.assertEqual(precheck.depends_on, ["await_pr_checks"])
        self.assertEqual(
            precheck.barrier,
            {
                "hook": "review_precheck",
                "pr_url": "$prior.deliver_work_order.output.pr_url",
                "work_order_body": "$workflow.work_order_body",
                "refresh": "$prior.deliver_work_order.output.refresh_method",
            },
        )
        self.assertEqual(precheck.on_complete, {"hook": "hydrate_input", "downstream": ["code_review"]})
        self.assertFalse(precheck.on_block)
        review = skeleton.stage("code_review")
        self.assertEqual((review.agent_id, review.allowed_agents), ("review-agent", ["review-agent"]))
        self.assertEqual(review.skill_hints, ["governed-code-review"])
        self.assertEqual(review.input_map["files"], "$prior.review_precheck.output.files")
        self.assertEqual(review.input_map["work_order_body"], "$workflow.work_order_body")
        contract = skeleton.resolve_stage_contract("code_review")
        self.assertEqual(contract["semantic_validators"], ["validate_code_review_envelope"])
        self.assertFalse(contract.get("outcome"))
        finalize = skeleton.stage("finalize_code_review")
        self.assertEqual(finalize.depends_on, ["code_review"])
        on_block = finalize.on_block
        self.assertEqual(on_block["max_revisions"], 3)
        self.assertNotIn("budget", on_block["revise"])
        self.assertEqual(on_block["revise"]["input_map"]["review_feedback"], "$reject.comments")
        self.assertEqual(
            [(step["step_name"], step["depends_on"]) for step in on_block["resume_after"]],
            REVIEW_CHAIN,
        )

    def test_materialize_seeds_work_order(self):
        skeleton = load_skeleton("deliver_work_order")
        tasks = materialize_skeleton_tasks(
            skeleton,
            workflow_id="wf-impl-wo",
            stage_inputs={
                "deliver_work_order": {
                    "work_order_id": "WO-001",
                    "work_order_ref": "docs/planner/WO-001.md",
                },
            },
        )
        deliver = next(task for task in tasks if task["step_name"] == "deliver_work_order")
        self.assertEqual(deliver["input"]["work_order_id"], "WO-001")
        qa = next(task for task in tasks if task["step_name"] == "qa_gate")
        self.assertTrue(qa["contract"])
        self.assertEqual(qa["definition_version"], "1.5.0")

    def test_materialize_leaves_compensation_to_the_failure_hook(self):
        skeleton = load_skeleton("deliver_work_order")
        self.assertEqual(skeleton.stage("record_implement_failure").kind, "compensate")
        tasks = materialize_skeleton_tasks(skeleton, workflow_id="wf-impl-wo")
        self.assertNotIn("record_implement_failure", [task["step_name"] for task in tasks])


class _SiblingMemory:
    """Two deliver_work_order workflows, each parked on its human merge wait."""

    def __init__(self, workflows):
        self.workflows = workflows
        self.created: list[dict] = []
        self.cancelled: list[str] = []

    def _task(self, task_id):
        return next(t for wf in self.workflows.values() for t in wf["tasks"] if t["id"] == task_id)

    def get_workflow(self, workflow_id):
        return self.workflows[workflow_id]

    def find_external_waits(self, *, event_type="", pr_url="", tenant_id="default"):
        waits = []
        for workflow_id, wf in self.workflows.items():
            for task in wf["tasks"]:
                if task["status"] == "blocked" and str(task.get("error", "")).startswith("awaiting_external:"):
                    waits.append({"workflow_id": workflow_id, "task_id": task["id"], "match_pr_urls": [wf["pr_url"]]})
        return {"waits": waits}

    def block_task_api(self, task_id, error, output=None, *, actor=""):
        task = self._task(task_id)
        task.update(status="blocked", error=error, output=output or task.get("output") or {})
        return {"id": task_id, "status": "blocked"}

    def cancel_task(self, task_id, *args, **kwargs):
        self._task(task_id)["status"] = "cancelled"
        self.cancelled.append(task_id)
        return {"status": "cancelled"}

    def create_tasks_bulk(self, payload):
        wf = self.workflows[payload["workflow_id"]]
        ids = []
        for spec in payload["tasks"]:
            task_id = f"{payload['workflow_id']}-new-{len(self.created)}"
            task = {"id": task_id, "workflow_id": payload["workflow_id"], "status": "pending", **spec}
            wf["tasks"].append(task)
            self.created.append(task)
            ids.append(task_id)
        return {"task_ids": ids}

    def acquire_workflow_lock(self, workflow_id, *, owner, ttl_seconds):
        return True

    def release_workflow_lock(self, workflow_id, *, owner):
        return True

    def __getattr__(self, name):
        return MagicMock(return_value={})


def _sibling_workflow(workflow_id, pr_url, work_order_id):
    deliver_input = {"work_order_id": work_order_id, "work_order_body": f"# {work_order_id}", "qa_feedback": "old"}
    tasks = [
        {"id": f"{workflow_id}-deliver", "step_name": "deliver_work_order", "status": "succeeded",
         "input": deliver_input, "output": {"pr_url": pr_url, "work_order_id": work_order_id}, "created_at": "1"},
        {"id": f"{workflow_id}-checks", "step_name": "await_pr_checks", "status": "succeeded", "created_at": "2"},
        {"id": f"{workflow_id}-qa", "step_name": "qa_gate", "status": "succeeded", "created_at": "3",
         "output": {"status": "success", "result": {"prs": [{"pr_url": pr_url}]}}},
        {"id": f"{workflow_id}-fresh", "step_name": "ensure_fresh", "status": "succeeded", "created_at": "4"},
        {"id": f"{workflow_id}-merge", "step_name": "await_human_merge", "status": "blocked",
         "error": "awaiting_external:github.merge", "created_at": "5"},
    ]
    for task in tasks:
        task.update(workflow_id=workflow_id, workflow_type="deliver_work_order", tenant_id="acme")
    return {
        "workflow": {"id": workflow_id, "workflow_type": "deliver_work_order", "tenant_id": "acme"},
        "tasks": tasks,
        "pr_url": pr_url,
    }


class SiblingRefreshEndToEndTests(unittest.TestCase):
    def test_merge_refreshes_same_base_sibling(self):
        from ts_harness.workflows.engine import WorkflowEngine
        from ts_harness.workflows.hooks import build_default_registry
        from ts_harness.workflows.merge_readiness import fan_out_base_advanced

        merged, sibling = "https://github.com/acme/app/pull/1", "https://github.com/acme/app/pull/2"
        memory = _SiblingMemory(
            {"wf-a": _sibling_workflow("wf-a", merged, "WO-001"), "wf-b": _sibling_workflow("wf-b", sibling, "WO-002")}
        )
        memory.workflows["wf-a"]["tasks"][-1]["status"] = "succeeded"
        github = MagicMock()
        github.get_pull.return_value = {"state": "open", "base": {"ref": "main"}, "head": {"sha": "b1"}}
        github.compare.return_value = {"behind_by": 1}
        engine = WorkflowEngine(client=memory, registry=build_default_registry())
        with patch.object(engine, "_maybe_notify", return_value={}), patch.object(engine, "_sync_session"):
            blocked = fan_out_base_advanced(
                {"pr_url": merged, "base_branch": "main"}, tenant_id="acme", memory=memory, github=github, engine=engine
            )

        self.assertEqual([item["workflow_id"] for item in blocked], ["wf-b"])
        self.assertIn("wf-b-merge", memory.cancelled)
        steps = [task["step_name"] for task in memory.created]
        self.assertEqual(
            steps,
            [
                "deliver_work_order",
                "await_pr_checks",
                "review_precheck",
                "code_review",
                "finalize_code_review",
                "qa_gate",
                "ensure_fresh",
                "await_human_merge",
            ],
        )
        refresh = memory.created[0]["input"]
        self.assertTrue(refresh["refresh"])
        self.assertEqual(refresh["_revision_budget"], "refresh")
        self.assertEqual(refresh["pr_url"], sibling)
        self.assertNotIn("qa_feedback", refresh)
        self.assertTrue(all(task["workflow_id"] == "wf-b" for task in memory.created))


class CodeReviewRevisionEndToEndTests(unittest.TestCase):
    PR = "https://github.com/acme/app/pull/3"

    def _add(self, memory, step, status, output=None):
        wf = memory.workflows["wf-r"]
        task = {
            "id": f"wf-r-{step}-{len(wf['tasks'])}",
            "step_name": step,
            "status": status,
            "output": output or {},
            "created_at": f"{len(wf['tasks']):03d}",
            "workflow_id": "wf-r",
            "workflow_type": "deliver_work_order",
            "tenant_id": "acme",
        }
        wf["tasks"].append(task)
        return task

    def _review(self, findings):
        return {
            "result": {
                "verdict": "block" if findings else "pass",
                "summary": "review",
                "prs": [{"pr_url": self.PR, "head_sha": "h1"}],
                "findings": findings,
            }
        }

    def _run_review(self, memory, github, skeleton, review_output):
        from ts_harness.workflows import code_review

        precheck_task = self._add(memory, "review_precheck", "running")
        precheck = code_review.review_precheck(
            precheck_task, skeleton.stage("review_precheck").barrier, memory=memory, github=github, program_defaults={}
        )
        precheck_task.update(status="succeeded", output=precheck)
        self._add(memory, "code_review", "succeeded", review_output)
        finalize_task = self._add(memory, "finalize_code_review", "running")
        return finalize_task, code_review.finalize_code_review(
            finalize_task, skeleton.stage("finalize_code_review").barrier, memory=memory, github=github
        )

    def test_review_block_revises_deliver_then_passes_to_qa(self):
        from ts_harness.workflows.engine import WorkflowEngine
        from ts_harness.workflows.hooks import build_default_registry
        from ts_harness.workflows.system_steps import StepBlocked

        memory = _SiblingMemory(
            {"wf-r": {"workflow": {"id": "wf-r", "workflow_type": "deliver_work_order", "tenant_id": "acme"}, "tasks": []}}
        )
        deliver = self._add(memory, "deliver_work_order", "succeeded", {"pr_url": self.PR, "work_order_id": "WO-3"})
        deliver["input"] = {"work_order_id": "WO-3", "work_order_body": "# WO-3\n\nBody"}
        self._add(memory, "await_pr_checks", "succeeded", {"checks_state": "success"})
        github = MagicMock()
        github.get_pull.return_value = {"state": "open", "head": {"sha": "h1"}, "base": {"ref": "main"}}
        github.list_pull_files.return_value = [
            {"filename": "src/a.py", "status": "modified", "additions": 4, "deletions": 1, "patch": "@@ +1 @@"}
        ]
        skeleton = load_skeleton("deliver_work_order")
        blocking = {
            "severity": "major",
            "category": "correctness",
            "pr_url": self.PR,
            "file": "src/a.py",
            "line": 2,
            "message": "Null input crashes the handler",
        }

        with self.assertRaises(StepBlocked) as blocked:
            self._run_review(memory, github, skeleton, self._review([blocking]))
        finalize_task = memory.workflows["wf-r"]["tasks"][-1]
        output = blocked.exception.output
        engine = WorkflowEngine(client=memory, registry=build_default_registry())
        with patch.object(engine, "_maybe_notify", return_value={}), patch.object(engine, "_sync_session"):
            engine.on_task_transition(
                {**finalize_task, "status": "blocked", "output": output},
                "blocked",
                dispatch_result={"output": output},
                error=f"barrier_blocked: {blocked.exception}",
            )

        self.assertEqual(
            [task["step_name"] for task in memory.created],
            [
                "deliver_work_order",
                "await_pr_checks",
                "review_precheck",
                "code_review",
                "finalize_code_review",
                "qa_gate",
                "ensure_fresh",
                "await_human_merge",
            ],
        )
        revision = memory.created[0]
        self.assertIn("Null input crashes the handler", revision["input"]["review_feedback"])
        self.assertEqual(revision["input"]["pr_url"], self.PR)
        self.assertNotIn("_revision_budget", revision["input"])
        self.assertEqual(memory.created[3]["agent_id"], "review-agent")

        for task in memory.created[:2]:
            task.update(status="succeeded", created_at=f"1{task['step_name']}")
        memory.created[0]["output"] = {"pr_url": self.PR, "work_order_id": "WO-3"}
        _task, passed = self._run_review(memory, github, skeleton, self._review([]))
        self.assertEqual(passed["verdict"], "pass")
        qa = memory.created[5]
        self.assertEqual((qa["step_name"], qa["depends_on"]), ("qa_gate", [memory.created[4]["id"]]))
        self.assertTrue(all(call.kwargs.get("body") for call in github.create_review.call_args_list))


if __name__ == "__main__":
    unittest.main()
