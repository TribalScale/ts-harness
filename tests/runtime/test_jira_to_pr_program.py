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
    "jira-to-pr",
    "verified-merge-governed",
    "implement-work-order",
    "governed-qa-gate",
    "governed-code-review",
}
AGENTS = {"orchestrator", "dev-agent", "qa-agent", "review-agent"}


class JiraToPrProgramTests(unittest.TestCase):
    def test_pick_and_implement_validate(self):
        hooks = set(build_default_registry().names())
        for name in ("pick_assigned_jira.json", "implement_jira_ticket.json"):
            payload = json.loads((WORKFLOWS / name).read_text())
            _, issues = validate_workflow_definition(
                payload,
                hook_names=hooks,
                agent_ids=AGENTS,
                skill_ids=SKILL_IDS,
                built_in_types={"implement_work_orders"},
            )
            errors = [issue for issue in issues if issue.severity == "error"]
            self.assertEqual(errors, [], errors)

    def test_entry_rules(self):
        loaded = ProgramDefinitionRegistry().load("jira_to_pr")
        self.assertEqual(
            validate_entry_against_snapshot(loaded, "pick", {}, require_startable=True),
            [],
        )
        missing = validate_entry_against_snapshot(
            loaded, "implement", {}, require_startable=True
        )
        self.assertTrue(any("jira_issue_key" in item for item in missing))
        self.assertEqual(
            validate_entry_against_snapshot(
                loaded,
                "implement",
                {"jira_issue_key": "TFS-184"},
                require_startable=True,
            ),
            [],
        )

    def test_program_activates(self):
        program = json.loads((PROGRAMS / "jira_to_pr.json").read_text())
        program["program_type"] = "jira_to_pr_ui"
        with tempfile.TemporaryDirectory() as tmp:
            store = ProgramDefinitionStore(Path(tmp))
            draft = store.create_draft(program, actor_id="admin")
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["program_type"], "jira_to_pr_ui")

    def test_pick_adapter(self):
        registry = build_default_adapter_registry()
        patch = registry.extract_outputs(
            "extract_pick_ticket_outputs",
            workflow_type="pick_assigned_jira",
            tasks=[
                {
                    "status": "succeeded",
                    "output": {"jira_issue_key": "TFS-184", "jira_project_key": "TFS"},
                }
            ],
        )
        self.assertEqual(patch["jira_issue_key"], "TFS-184")

    def test_routing_intent_distinct_from_dev_work(self):
        payload = {
            "definition_type": "channel_routing",
            "definition_version": "1.0.0",
            "intents": [
                {
                    "id": "jira_to_pr",
                    "signals": ["my tickets", "pick up my jira", "implement TFS-"],
                    "program_type": "jira_to_pr",
                    "entry_phase": "pick",
                    "policy": {
                        "mode": "require_workflow",
                        "deny_tools": ["propose_delegate_task", "delegate_task"],
                    },
                }
            ],
        }
        canonical, issues = validate_overlay_payload(
            payload,
            known_program_types={"jira_to_pr"},
            known_workflow_types={"pick_assigned_jira"},
        )
        self.assertIsNotNone(canonical)
        self.assertFalse(any(issue["severity"] == "error" for issue in issues))
        self.assertNotIn("fix bug", canonical["intents"][0]["signals"])
        self.assertNotIn("open pr", canonical["intents"][0]["signals"])


if __name__ == "__main__":
    unittest.main()
