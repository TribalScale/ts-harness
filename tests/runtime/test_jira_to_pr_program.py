from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.routing.intents import program_advance_candidates
from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.program_adapters import build_default_adapter_registry
from ts_harness.workflows.program_definition_store import ProgramDefinitionStore
from ts_harness.workflows.program_definition_validation import validate_program_definition
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
                {"jira_issue_key": "PROJ-184"},
                require_startable=True,
            ),
            [],
        )
        from ts_harness.workflows.program_materialize import seed_list_reentry

        seeded = seed_list_reentry(loaded, "implement", {"jira_issue_key": "PROJ-184"})
        self.assertEqual(seeded["jira_issue_key"], "PROJ-184")
        self.assertNotIn("jira_issue_keys", seeded)

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
                    "output": {"jira_issue_key": "PROJ-184", "jira_project_key": "PROJ"},
                }
            ],
        )
        self.assertEqual(patch["jira_issue_key"], "PROJ-184")
        self.assertNotIn("jira_issue_keys", patch)

    def test_pick_adapter_keeps_a_string_list(self):
        registry = build_default_adapter_registry()
        patch = registry.extract_outputs(
            "extract_pick_ticket_outputs",
            workflow_type="pick_assigned_jira",
            tasks=[
                {
                    "status": "succeeded",
                    "output": {
                        "jira_issue_key": "PROJ-1",
                        "jira_issue_keys": ["PROJ-1", "PROJ-2"],
                        "queue_exclusions": [{"key": "PROJ-9", "reason": "blocked by PROJ-8"}],
                    },
                }
            ],
        )
        self.assertEqual(patch["jira_issue_keys"], ["PROJ-1", "PROJ-2"])
        self.assertNotIn("queue_exclusions", patch)
        dropped = registry.extract_outputs(
            "extract_pick_ticket_outputs",
            workflow_type="pick_assigned_jira",
            tasks=[{"status": "succeeded", "output": {"jira_issue_key": "PROJ-1", "jira_issue_keys": ["PROJ-1", 2]}}],
        )
        self.assertNotIn("jira_issue_keys", dropped)

    def test_program_reentry_has_no_errors(self):
        payload = json.loads((PROGRAMS / "jira_to_pr.json").read_text())
        _, issues = validate_program_definition(
            payload,
            workflow_catalog={
                "pick_assigned_jira": {
                    "workflow_type": "pick_assigned_jira",
                    "status": "built_in",
                    "available_versions": ["1.1.0"],
                    "resolvable": True,
                    "active_version": "1.1.0",
                    "contract_output_keys": {"jira_issue_key", "jira_issue_keys"},
                },
                "implement_jira_ticket": {
                    "workflow_type": "implement_jira_ticket",
                    "status": "built_in",
                    "available_versions": ["1.0.0"],
                    "resolvable": True,
                    "active_version": "1.0.0",
                    "contract_output_keys": {"pr_url"},
                },
            },
            adapter_names={"extract_pick_ticket_outputs", "extract_implement_outputs"},
            built_in_types={"jira_to_pr"},
            allow_builtin_type=True,
        )
        reentry_errors = [
            issue for issue in issues if issue.severity == "error" and issue.code.startswith("reentry")
        ]
        self.assertEqual(reentry_errors, [], reentry_errors)

    def test_active_queue_hides_the_next_ticket_edge(self):
        base = {
            "program_id": "prog-1",
            "program_type": "jira_to_pr",
            "program_phase": "implement",
            "workflow_type": "implement_jira_ticket",
        }
        active = {
            **base,
            "program_artifacts": {
                "jira_issue_keys": ["PROJ-1", "PROJ-2"],
                "completed_jira_issue_keys": ["PROJ-1"],
            },
        }
        self.assertEqual(program_advance_candidates("next ticket", active), [])
        finished = {
            **base,
            "program_artifacts": {
                "jira_issue_keys": ["PROJ-1"],
                "completed_jira_issue_keys": ["PROJ-1"],
            },
        }
        targets = [item["target_node"] for item in program_advance_candidates("show my tickets", finished)]
        self.assertEqual(targets, ["pick"])
        single = {**base, "program_artifacts": {"jira_issue_key": "PROJ-184"}}
        targets = [item["target_node"] for item in program_advance_candidates("remaining tickets", single)]
        self.assertEqual(targets, ["pick"])

    def test_routing_intent_distinct_from_dev_work(self):
        payload = {
            "definition_type": "channel_routing",
            "definition_version": "1.0.0",
            "intents": [
                {
                    "id": "jira_to_pr",
                    "signals": ["my tickets", "pick up my jira", r"implement [a-z][a-z0-9]+-\d+"],
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


class _QueueMemory:
    def __init__(self, artifacts, tasks):
        self.artifacts = dict(artifacts)
        self.tasks = tasks
        self.programs_completed = []

    def get_workflow(self, workflow_id):
        return {
            "workflow": {
                "id": workflow_id,
                "program_id": "prog-1",
                "program_phase": "implement",
                "summary": "implement the rest",
                "source_channel": "slack:C1:1",
            },
            "tasks": self.tasks,
        }

    def get_program(self, program_id):
        return {
            "program": {
                "id": program_id,
                "current_phase": "implement",
                "program_definition_snapshot": {
                    "nodes": [
                        {
                            "id": "implement",
                            "terminal": True,
                            "required_artifacts": ["jira_issue_key"],
                            "produces_artifacts": ["pr_url"],
                            "complete_channel_text": "This ticket is merged.",
                            "reentry": {
                                "target_node": "implement",
                                "mode": "list",
                                "remaining_list_artifact": "jira_issue_keys",
                                "completed_list_artifact": "completed_jira_issue_keys",
                                "next_item_artifact": "jira_issue_key",
                                "max_iterations": 50,
                            },
                        }
                    ],
                    "edges": [],
                },
                "metadata": {"artifacts": self.artifacts},
            }
        }

    def patch_program_artifacts(self, program_id, artifacts, *, merge=True, actor=""):
        self.artifacts.update(artifacts)
        return {}

    def complete_workflow(self, workflow_id, **kwargs):
        return {}

    def complete_program(self, program_id, *, actor, phase=""):
        self.programs_completed.append(phase)
        return {}

    def get_program_status(self, program_id):
        return {"children": []}


class TicketQueuePhaseTests(unittest.TestCase):
    def _ctx(self, memory):
        from types import SimpleNamespace

        from ts_harness.workflows.registry import HookContext

        return HookContext(
            workflow_id="wf-1",
            workflow_type="implement_jira_ticket",
            tenant_id="default",
            task={"id": "t-1", "status": "succeeded"},
            stage=None,
            skeleton=SimpleNamespace(program={}),
            actor="workflow-runner",
            memory_client=memory,
            extras={},
        )

    def test_two_keys_reenter_implement_with_the_second_key(self):
        from unittest.mock import MagicMock, patch

        from ts_harness.workflows.hooks import complete_program_phase

        memory = _QueueMemory(
            {"jira_issue_keys": ["PROJ-1", "PROJ-2"], "jira_issue_key": "PROJ-1"},
            [{"status": "succeeded", "output": {}}],
        )
        service = MagicMock()
        service.on_workflow_terminal.return_value = {}
        service.advance_program.return_value = {"workflow_id": "wf-2"}
        with patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
            return_value=service,
        ):
            result = complete_program_phase.handle(self._ctx(memory))
        self.assertEqual(result["status"], "phase_reentered")
        self.assertEqual(service.advance_program.call_args.kwargs["target_phase"], "implement")
        self.assertEqual(
            service.advance_program.call_args.kwargs["artifacts"],
            {"jira_issue_key": "PROJ-2", "reentry_index": 1},
        )
        self.assertEqual(memory.programs_completed, [])
        self.assertNotEqual(memory.artifacts.get("program_status"), "succeeded")

    def test_failed_task_stops_without_advance(self):
        from unittest.mock import patch

        from ts_harness.workflows.hooks import complete_program_phase

        memory = _QueueMemory(
            {"jira_issue_keys": ["PROJ-1", "PROJ-2"], "jira_issue_key": "PROJ-1"},
            [{"status": "failed", "step_name": "deliver_work_order"}],
        )
        with patch("ts_harness.workflows.hooks.complete_program_phase._notify_channel") as notify, patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service"
        ) as service:
            result = complete_program_phase.handle(self._ctx(memory))
        self.assertEqual(result["status"], "phase_failed")
        service.assert_not_called()
        self.assertIn("PROJ-1", notify.call_args.args[2])
        self.assertIn("PROJ-2", notify.call_args.args[2])
        self.assertEqual(memory.programs_completed, [])


if __name__ == "__main__":
    unittest.main()
