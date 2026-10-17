from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.workflows.definition_validation import validate_workflow_definition
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
                        "jira_epic_key": "TFS-100",
                        "jira_story_keys": ["TFS-184"],
                    },
                }
            ],
        )
        self.assertEqual(patch["jira_epic_key"], "TFS-100")
        self.assertEqual(patch["jira_story_keys"], ["TFS-184"])

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
