from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.integrations.github_client import GitHubError
from ts_harness.jira.program_config import resolved_program_config
from ts_harness.workflows.bug_context import (
    FILE_LIMIT,
    TREE_PATH_LIMIT,
    cited_paths,
    gather_bug_context,
)
from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.expressions import resolve_token
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.program_materialize import build_intake_for_node
from ts_harness.workflows.program_registry import ProgramDefinitionRegistry
from ts_harness.workflows.system_steps import run_system_step

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "config" / "workflows"

SKILL_IDS = {"bug-intake", "investigate-bug", "file-work-orders-as-jira"}
AGENTS = {"orchestrator", "pm-agent", "review-agent"}


class _Memory:
    def __init__(self, tasks: list[dict]):
        self.tasks = tasks

    def get_workflow(self, workflow_id: str) -> dict:
        return {"workflow": {"id": workflow_id}, "tasks": self.tasks}


class _GitHub:
    def __init__(self, paths: list[str], files: dict[str, str]):
        self.paths = paths
        self.files = files
        self.fetched: list[str] = []

    def get_tree(self, owner: str, repo: str, ref: str, *, recursive: bool = True) -> dict:
        return {"tree": [{"path": path, "type": "blob"} for path in self.paths]}

    def get_file(self, owner: str, repo: str, path: str, *, ref: str) -> dict:
        self.fetched.append(path)
        body = self.files[path]
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(body.encode()).decode(),
        }


class BugToJiraProgramTests(unittest.TestCase):
    def test_registry_lists_program_and_entry_rules(self):
        registry = ProgramDefinitionRegistry()
        self.assertIn("bug_to_jira_work_orders", registry.list_types())
        loaded = registry.load("bug_to_jira_work_orders")
        from ts_harness.workflows.program_materialize import validate_entry_against_snapshot

        self.assertEqual(
            validate_entry_against_snapshot(loaded, "bug_intake", {}, require_startable=True),
            [],
        )
        missing = validate_entry_against_snapshot(loaded, "file_jira", {}, require_startable=True)
        self.assertTrue(any("work_order_ids" in item for item in missing))
        self.assertTrue(any("work_orders_markdown" in item for item in missing))
        ok = validate_entry_against_snapshot(
            loaded,
            "file_jira",
            {"work_order_ids": ["WO-001"], "work_orders_markdown": "# WO-001\n"},
            require_startable=True,
        )
        self.assertEqual(ok, [])
        node_ids = [node["id"] for node in loaded["nodes"]]
        self.assertEqual(node_ids, ["bug_intake", "work_order", "file_jira"])
        self.assertNotIn("implement", node_ids)

    def test_workflows_validate_without_coding_backend_or_refinery(self):
        hooks = set(build_default_registry().names())
        for name in ("bug_to_plan.json", "bug_work_order.json"):
            payload = json.loads((WORKFLOWS / name).read_text())
            _, issues = validate_workflow_definition(
                payload,
                hook_names=hooks,
                agent_ids=AGENTS,
                skill_ids=SKILL_IDS,
                built_in_types=set(),
            )
            errors = [issue for issue in issues if issue.severity == "error"]
            self.assertEqual(errors, [], errors)
            blob = json.dumps(payload)
            self.assertNotIn("run_coding_backend", blob)
            self.assertNotIn("refinery", blob)
            self.assertNotIn("foundry", blob)

        plan = json.loads((WORKFLOWS / "bug_to_plan.json").read_text())
        investigate = next(stage for stage in plan["stages"] if stage["name"] == "investigate")
        self.assertEqual(investigate["agent_id"], "review-agent")
        self.assertEqual(investigate["skill_hints"], ["investigate-bug"])
        self.assertEqual(plan["session"]["artifact_steps"], ["draft_bug"])
        produces = json.loads((ROOT / "config" / "programs" / "bug_to_jira_work_orders.json").read_text())
        intake = next(node for node in produces["nodes"] if node["id"] == "bug_intake")
        self.assertEqual(intake["produces_artifacts"][0], "bug_report")

    def test_approved_bug_report_reaches_the_filing_stage(self):
        bug = ProgramDefinitionRegistry().load("bug_to_jira_work_orders")
        report = "Checkout total is wrong\n\n## Steps to reproduce\n1. Add two items"
        intake = build_intake_for_node(
            snapshot=bug,
            node_id="file_jira",
            workflow_type="file_work_orders_as_jira",
            summary="file the bug",
            artifacts={"work_order_ids": ["WO-001"], "work_orders_markdown": "# WO-001\n", "bug_report": report},
        )
        self.assertEqual(intake["bug_report"], report)
        filing = json.loads((WORKFLOWS / "file_work_orders_as_jira.json").read_text())
        file_tickets = next(stage for stage in filing["stages"] if stage["name"] == "file_tickets")
        self.assertEqual(file_tickets["input_map"]["bug_report"], "$workflow.bug_report")
        tasks = [{"step_name": "choose_epic", "status": "pending", "created_at": "1", "input": intake}]
        self.assertEqual(resolve_token("$workflow.bug_report", tasks=tasks), report)

    def test_resolved_issue_type_is_bug_and_content_stays_story(self):
        registry = ProgramDefinitionRegistry()
        bug = registry.load("bug_to_jira_work_orders")
        content = registry.load("content_to_jira_work_orders")
        self.assertEqual(resolved_program_config(bug)["jira_filing"]["default_issue_type"], "Bug")
        self.assertEqual(resolved_program_config(content)["jira_filing"]["default_issue_type"], "Story")
        artifacts = {"work_order_ids": ["WO-001"], "work_orders_markdown": "# WO-001\n"}
        bug_intake = build_intake_for_node(
            snapshot=bug,
            node_id="file_jira",
            workflow_type="file_work_orders_as_jira",
            summary="file the bug",
            artifacts=artifacts,
        )
        content_intake = build_intake_for_node(
            snapshot=content,
            node_id="file_jira",
            workflow_type="file_work_orders_as_jira",
            summary="file the stories",
            artifacts=artifacts,
        )
        self.assertEqual(bug_intake["default_issue_type"], "Bug")
        self.assertEqual(bug_intake["issue_type_by_project"], {})
        self.assertEqual(content_intake["default_issue_type"], "Story")
        filing = json.loads((WORKFLOWS / "file_work_orders_as_jira.json").read_text())
        file_tickets = next(stage for stage in filing["stages"] if stage["name"] == "file_tickets")
        self.assertEqual(file_tickets["input_map"]["default_issue_type"], "$workflow.default_issue_type")
        self.assertEqual(
            resolve_token(
                "$workflow.default_issue_type",
                tasks=[
                    {
                        "step_name": "choose_epic",
                        "status": "pending",
                        "created_at": "1",
                        "input": {"default_issue_type": "Bug", "issue_type_by_project": {"PROJ": "Bug"}},
                    }
                ],
            ),
            "Bug",
        )
        self.assertEqual(
            resolve_token(
                "$workflow.issue_type_by_project",
                tasks=[
                    {
                        "step_name": "choose_epic",
                        "status": "pending",
                        "created_at": "1",
                        "input": {"issue_type_by_project": {}},
                    }
                ],
            ),
            {},
        )


class GatherBugContextTests(unittest.TestCase):
    def test_returns_tree_and_cited_file_bodies(self):
        paths = [f"src/f{index}.py" for index in range(TREE_PATH_LIMIT + 10)]
        paths.extend(["src/billing.py", "src/other.py"])
        github = _GitHub(paths, {"src/billing.py": "def charge():\n    return 1\n", "src/other.py": "x = 1\n"})
        task = {
            "id": "gather",
            "workflow_id": "wf",
            "step_name": "gather_context",
            "input": {"summary": "checkout crashes in src/billing.py and src/missing.py"},
        }
        output = gather_bug_context(
            task,
            {"repo": "acme/checkout", "default_branch": "dev"},
            memory=_Memory([]),
            github=github,
        )
        self.assertEqual(output["repo"], "acme/checkout")
        self.assertEqual(output["default_branch"], "dev")
        self.assertEqual(len(output["tree"]), TREE_PATH_LIMIT)
        self.assertEqual(output["tree"][0], "src/billing.py")
        self.assertEqual(github.fetched, ["src/billing.py"])
        self.assertEqual([item["path"] for item in output["files"]], ["src/billing.py"])
        github.fetched.clear()

        github.paths = ["src/billing.py", "src/other.py", "README.md"]
        output = gather_bug_context(
            task,
            {"repo": "acme/checkout", "default_branch": "dev"},
            memory=_Memory([]),
            github=github,
        )
        self.assertEqual([item["path"] for item in output["files"]], ["src/billing.py"])
        self.assertIn("def charge", output["files"][0]["body"])
        self.assertEqual(github.fetched, ["src/billing.py"])
        self.assertNotIn("src/missing.py", github.fetched)

    def test_bug_stages_receive_the_full_report(self):
        report = "x" * 300 + " the bug is in runtime/ts_harness/gateway/runtime_settings_bff.py"
        tasks = [
            {
                "step_name": "pick_repo",
                "status": "succeeded",
                "created_at": "1",
                "input": {"summary": report},
                "output": {"summary": "Approved pick_repo", "repo": "acme/app"},
            }
        ]
        self.assertEqual(resolve_token("$prior.pick_repo.input.summary", tasks=tasks), report)
        self.assertEqual(resolve_token("$prior.pick_repo.input", tasks=tasks), {"summary": report})
        self.assertEqual(resolve_token("$prior.pick_repo.output.repo", tasks=tasks), "acme/app")
        plan = json.loads((WORKFLOWS / "bug_to_plan.json").read_text())
        for stage in plan["stages"]:
            if stage["name"] in {"investigate", "clarify", "draft_bug"}:
                self.assertEqual(stage["input_map"]["report"], "$prior.pick_repo.input.summary", stage["name"])

    def test_caps_cited_files(self):
        paths = [f"pkg/m{index}.py" for index in range(FILE_LIMIT + 2)]
        report = " ".join(paths)
        self.assertEqual(cited_paths(report, paths), paths[:FILE_LIMIT])

    def test_missing_repo_blocks(self):
        result = run_system_step(
            "gather_bug_context",
            {"id": "gather", "workflow_id": "wf", "input": {"summary": "it broke"}},
            {"hook": "gather_bug_context"},
            memory=_Memory([]),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no repo", result["reason"])

    def test_github_error_blocks(self):
        class Broken:
            def get_tree(self, *args, **kwargs):
                raise GitHubError(500, "unavailable")

        with patch(
            "ts_harness.workflows.bug_context.GitHubClient.from_env",
            return_value=Broken(),
        ):
            result = run_system_step(
                "gather_bug_context",
                {"id": "gather", "workflow_id": "wf", "input": {"summary": "src/billing.py"}},
                {"hook": "gather_bug_context", "repo": "acme/checkout", "default_branch": "dev"},
                memory=_Memory([]),
            )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unavailable", result["reason"])


class FillStageInputsTests(unittest.TestCase):
    def test_unhydrated_stage_gets_its_input_map_at_dispatch(self):
        from ts_harness.workflows.hooks.hydrate_input import fill_missing_inputs
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton("bug_to_plan")
        report = "Settings previews the oldest draft; see runtime/app/settings_bff.py"
        tasks = [
            {"id": "p", "step_name": "pick_repo", "status": "succeeded", "created_at": "1",
             "input": {"summary": report}, "output": {"summary": "Approved pick_repo", "repo": "acme/app", "default_branch": "dev"}},
            {"id": "i", "step_name": "investigate", "status": "succeeded", "created_at": "2",
             "input": {}, "output": {"diagnosis": "drafts[0] wins"}},
        ]
        clarify = {"id": "c", "workflow_id": "wf", "step_name": "clarify", "input": {"repo": "kept/override"}}
        filled = fill_missing_inputs(_Memory(tasks), clarify, skeleton.stage("clarify"))
        self.assertEqual(filled["report"], report)
        self.assertEqual(filled["diagnosis"], "drafts[0] wins")
        self.assertEqual(filled["repo"], "kept/override")
        self.assertEqual(filled["default_branch"], "dev")
