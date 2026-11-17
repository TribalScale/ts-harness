from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.jira.target_project import jira_choice_from_reply
from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.expressions import resolve_input_map
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.program_adapters import build_default_adapter_registry
from ts_harness.workflows.program_definition_store import ProgramDefinitionStore
from ts_harness.workflows.program_materialize import validate_entry_against_snapshot
from ts_harness.workflows.program_registry import ProgramDefinitionRegistry
from ts_harness.routing.overlay import validate_overlay_payload

ROOT = Path(__file__).resolve().parents[2]
PROGRAMS = ROOT / "config" / "programs"
WORKFLOWS = ROOT / "config" / "workflows"

SKILL_IDS = {
    "content-to-plan",
    "file-work-orders-as-jira",
    "prd-to-work-orders-governed",
    "source-work-order-governed",
}
AGENTS = {"orchestrator", "pm-agent"}


class ContentToJiraProgramTests(unittest.TestCase):
    def test_content_to_plan_validates(self):
        payload = json.loads((WORKFLOWS / "content_to_plan.json").read_text())
        _, issues = validate_workflow_definition(
            payload,
            hook_names=set(build_default_registry().names()),
            agent_ids=AGENTS,
            skill_ids=SKILL_IDS,
            built_in_types={"prd_request"},
        )
        errors = [issue for issue in issues if issue.severity == "error"]
        self.assertEqual(errors, [], errors)

    def test_compare_codebase_receives_the_fetched_repo(self):
        plan = json.loads((WORKFLOWS / "content_to_plan.json").read_text())
        stages = {stage["name"]: stage for stage in plan["stages"]}
        gather = stages["gather_context"]
        self.assertEqual(gather["depends_on"], ["pick_repo"])
        self.assertEqual(gather["barrier"]["hook"], "gather_bug_context")
        compare = stages["compare_codebase"]
        self.assertEqual(compare["depends_on"], ["gather_context"])
        self.assertEqual(compare["input_map"]["tree"], "$prior.gather_context.output.tree")
        self.assertEqual(compare["input_map"]["files"], "$prior.gather_context.output.files")
        self.assertEqual(compare["input_map"]["directories"], "$prior.gather_context.output.directories")
        bug = json.loads((WORKFLOWS / "bug_to_plan.json").read_text())
        investigate = next(stage for stage in bug["stages"] if stage["name"] == "investigate")
        self.assertEqual(investigate["input_map"]["directories"], "$prior.gather_context.output.directories")
        for stage in (compare, investigate):
            self.assertEqual(stage["input_map"]["tree_truncated"], "$prior.gather_context.output.tree_truncated")
        self.assertNotEqual(plan["definition_version"], "1.1.0")

        from ts_harness.workflows.hooks.hydrate_input import fill_missing_inputs
        from ts_harness.workflows.materialize import load_skeleton

        class _Memory:
            def __init__(self, tasks):
                self.tasks = tasks

            def get_workflow(self, workflow_id):
                return {"workflow": {"id": workflow_id}, "tasks": self.tasks}

        content = "Add a Last workflow line to admin-ui/src/settings/SystemSettings.tsx"
        tasks = [
            {"id": "p", "step_name": "pick_repo", "status": "succeeded", "created_at": "1",
             "input": {"summary": content}, "output": {"repo": "acme/app", "default_branch": "dev"}},
            {"id": "g", "step_name": "gather_context", "status": "succeeded", "created_at": "2", "input": {},
             "output": {"repo": "acme/app", "default_branch": "dev", "tree": ["admin-ui/src/settings/SystemSettings.tsx"],
                        "files": [{"path": "admin-ui/src/settings/SystemSettings.tsx", "body": "x", "truncated": False}]}},
        ]
        task = {"id": "c", "workflow_id": "wf", "step_name": "compare_codebase", "input": {}}
        filled = fill_missing_inputs(_Memory(tasks), task, load_skeleton("content_to_plan").stage("compare_codebase"))
        self.assertEqual(filled["content"], content)
        self.assertEqual(filled["tree"], ["admin-ui/src/settings/SystemSettings.tsx"])
        self.assertEqual(filled["files"][0]["path"], "admin-ui/src/settings/SystemSettings.tsx")
        self.assertEqual(filled["repo"], "acme/app")

    def test_intake_sources_reach_every_intake_stage(self):
        from ts_harness.workflows.hooks.hydrate_input import fill_missing_inputs
        from ts_harness.workflows.materialize import load_skeleton

        class _Memory:
            def __init__(self, tasks):
                self.tasks = tasks

            def get_workflow(self, workflow_id):
                return {"workflow": {"id": workflow_id}, "tasks": self.tasks}

        cases = {
            "content_to_plan": ("gather_context", ["compare_codebase", "clarify", "draft_plan"], "1.4.0"),
            "bug_to_plan": ("gather_context", ["investigate", "clarify", "draft_bug"], "1.4.0"),
            "prd_request": ("gather_sources", ["intake", "draft_prd"], "1.6.0"),
            "rfp_request": ("gather_sources", ["intake", "draft_rfp"], "1.2.0"),
        }
        for workflow_type, (barrier_step, consumers, version) in cases.items():
            with self.subTest(workflow_type=workflow_type):
                skeleton = load_skeleton(workflow_type)
                self.assertEqual(skeleton.definition_version, version)
                barrier = skeleton.stage(barrier_step).barrier
                self.assertEqual(barrier["intake_sources"], "$workflow.intake_sources")
                if barrier_step == "gather_context":
                    self.assertIs(barrier["include_intake_sources"], True)
                else:
                    self.assertEqual(barrier["hook"], "gather_intake_sources")
                    self.assertEqual(skeleton.intake_stage_name(), "intake")
                    self.assertIn(barrier_step, skeleton.stage("intake").depends_on)
                self.assertEqual(
                    barrier["agent_id"], "sales-agent" if workflow_type == "rfp_request" else "pm-agent"
                )
                tasks = [{"id": "b", "step_name": barrier_step, "status": "succeeded", "created_at": "1",
                          "input": {}, "output": {"sources_text": "## Shared sources\nbrief"}}]
                for step in consumers:
                    task = {"id": step, "workflow_id": "wf", "step_name": step, "input": {}}
                    filled = fill_missing_inputs(_Memory(tasks), task, skeleton.stage(step))
                    self.assertEqual(filled["sources"], "## Shared sources\nbrief", step)
        # Filing and implementation never read the files.
        for workflow_type in ("file_work_orders_as_jira", "implement_jira_ticket"):
            text = (WORKFLOWS / f"{workflow_type}.json").read_text()
            self.assertNotIn("intake_sources", text)
            self.assertNotIn("sources_text", text)

    def test_prd_and_rfp_requests_validate(self):
        for name, agents, skills in (
            ("prd_request", {"orchestrator", "pm-agent"}, {"prd-intake", "prd-drafter"}),
            ("rfp_request", {"orchestrator", "sales-agent"}, set()),
        ):
            payload = json.loads((WORKFLOWS / f"{name}.json").read_text())
            _, issues = validate_workflow_definition(
                payload,
                hook_names=set(build_default_registry().names()),
                agent_ids=agents,
                skill_ids=skills,
                built_in_types=set(),
            )
            errors = [issue for issue in issues if issue.severity == "error"]
            self.assertEqual(errors, [], (name, errors))

    def test_file_work_orders_as_jira_validates(self):
        payload = json.loads((WORKFLOWS / "file_work_orders_as_jira.json").read_text())
        _, issues = validate_workflow_definition(
            payload,
            hook_names=set(build_default_registry().names()),
            agent_ids=AGENTS,
            skill_ids=SKILL_IDS,
            built_in_types={"jira_from_prd"},
        )
        errors = [issue for issue in issues if issue.severity == "error"]
        self.assertEqual(errors, [], errors)

    def test_builtin_program_loads_and_validates_entry(self):
        registry = ProgramDefinitionRegistry()
        loaded = registry.load("content_to_jira_work_orders")
        self.assertEqual(loaded["program_type"], "content_to_jira_work_orders")
        self.assertEqual(
            validate_entry_against_snapshot(loaded, "intake", {}, require_startable=True),
            [],
        )
        missing = validate_entry_against_snapshot(
            loaded, "file_jira", {}, require_startable=True
        )
        self.assertTrue(any("work_order_ids" in item for item in missing))
        ok = validate_entry_against_snapshot(
            loaded,
            "file_jira",
            {"work_order_ids": ["WO-001"], "work_orders_markdown": "# WO"},
            require_startable=True,
        )
        self.assertEqual(ok, [])

    def test_program_activates(self):
        program = json.loads((PROGRAMS / "content_to_jira_work_orders.json").read_text())
        program["program_type"] = "content_to_jira_work_orders_ui"
        with tempfile.TemporaryDirectory() as tmp:
            store = ProgramDefinitionStore(Path(tmp))
            draft = store.create_draft(program, actor_id="admin")
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["program_type"], "content_to_jira_work_orders_ui")

    def test_file_jira_adapter_extracts_keys(self):
        registry = build_default_adapter_registry()
        patch = registry.extract_outputs(
            "extract_file_jira_outputs",
            workflow_type="file_work_orders_as_jira",
            tasks=[
                {
                    "status": "succeeded",
                    "output": {
                        "jira_epic_key": "PROJ-100",
                        "jira_story_keys": ["PROJ-184"],
                    },
                }
            ],
        )
        self.assertEqual(patch["jira_epic_key"], "PROJ-100")
        self.assertEqual(patch["jira_story_keys"], ["PROJ-184"])

    def test_file_jira_adapter_reads_keys_from_the_summary(self):
        patch = build_default_adapter_registry().extract_outputs(
            "extract_file_jira_outputs",
            workflow_type="file_work_orders_as_jira",
            tasks=[
                {
                    "step_name": "file_tickets",
                    "status": "succeeded",
                    "output": {"summary": "Filed epic THD-19 with stories THD-20 and THD-21 for WO-001."},
                }
            ],
        )
        self.assertEqual(patch["jira_epic_key"], "THD-19")
        self.assertEqual(patch["jira_story_keys"], ["THD-20", "THD-21"])
        lone_bug = build_default_adapter_registry().extract_outputs(
            "extract_file_jira_outputs",
            workflow_type="file_work_orders_as_jira",
            tasks=[{"status": "succeeded", "output": {"summary": "Filed the bug as THD-22."}}],
        )
        self.assertEqual(lone_bug, {"jira_story_keys": ["THD-22"]})

    def test_filing_runs_once_the_epic_choice_is_confirmed(self):
        filing = json.loads((WORKFLOWS / "file_work_orders_as_jira.json").read_text())
        stages = {stage["name"]: stage for stage in filing["stages"]}
        self.assertTrue(stages["choose_epic"]["approval_required"])
        self.assertFalse(stages["file_tickets"].get("approval_required", False))
        self.assertNotEqual(filing["definition_version"], "1.1.0")

    def test_confirmed_project_reaches_the_filing_step(self):
        filing = json.loads((WORKFLOWS / "file_work_orders_as_jira.json").read_text())
        file_tickets = next(stage for stage in filing["stages"] if stage["name"] == "file_tickets")
        intake = {"jira_allowed_projects": ["TFS", "THD"], "jira_project_key": "THD"}
        choice = jira_choice_from_reply("ready", intake)
        tasks = [
            {
                "step_name": "choose_epic",
                "status": "succeeded",
                "created_at": "1",
                "input": intake,
                "output": {"reply_text": "ready", **choice},
            }
        ]
        resolved = resolve_input_map(file_tickets["input_map"], tasks=tasks)
        self.assertEqual(resolved["jira_project_key"], "THD")
        self.assertEqual(resolved["jira_allowed_projects"], ["TFS", "THD"])

    def test_reply_can_name_an_epic_or_another_allowed_project(self):
        intake = {"jira_allowed_projects": ["TFS", "THD"], "jira_project_key": "THD"}
        self.assertEqual(
            jira_choice_from_reply("file under THD-17", intake),
            {"jira_epic_key": "THD-17", "jira_project_key": "THD"},
        )
        self.assertEqual(jira_choice_from_reply("use tfs instead", intake), {"jira_project_key": "TFS"})
        self.assertEqual(jira_choice_from_reply("ready, file WO-001", intake), {"jira_project_key": "THD"})
        self.assertEqual(jira_choice_from_reply("ready", {"summary": "no jira here"}), {})

    def test_routing_intent(self):
        payload = {
            "definition_type": "channel_routing",
            "definition_version": "1.0.0",
            "intents": [
                {
                    "id": "content_to_jira",
                    "signals": ["plan work from this content"],
                    "program_type": "content_to_jira_work_orders",
                    "entry_phase": "intake",
                    "policy": {
                        "mode": "require_workflow",
                        "deny_tools": ["propose_delegate_task", "delegate_task"],
                    },
                }
            ],
        }
        canonical, issues = validate_overlay_payload(
            payload,
            known_program_types={"content_to_jira_work_orders"},
            known_workflow_types={"content_to_plan"},
        )
        self.assertIsNotNone(canonical)
        self.assertFalse(any(issue["severity"] == "error" for issue in issues))
        self.assertNotIn("create a prd", canonical["intents"][0]["signals"])


if __name__ == "__main__":
    unittest.main()
