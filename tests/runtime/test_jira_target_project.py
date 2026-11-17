from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.jira.target_project import resolve_jira_target, with_jira_target
from ts_harness.workflows.definition_validation import _validate_jira_targets


def _defaults(*projects: str) -> dict:
    return {"jira_filing": {"allowed_projects": list(projects)}}


class ResolveJiraTargetTests(unittest.TestCase):
    def test_explicit_key_wins(self):
        self.assertEqual(resolve_jira_target({"jira_project_key": "abc", "summary": "file in XYZ"}, ["XYZ"]), "ABC")

    def test_epic_key_names_the_project(self):
        self.assertEqual(resolve_jira_target({"jira_epic_key": "PAY-12"}, ["ABC", "PAY"]), "PAY")

    def test_single_allowed_project_named_in_request(self):
        self.assertEqual(resolve_jira_target({"summary": "create jira in pay please"}, ["ABC", "PAY"]), "PAY")

    def test_mention_must_be_a_whole_key(self):
        self.assertEqual(resolve_jira_target({"summary": "payment flow"}, ["ABC", "PAY"]), "")

    def test_two_mentions_are_ambiguous(self):
        self.assertEqual(resolve_jira_target({"summary": "ABC or PAY"}, ["ABC", "PAY"]), "")

    def test_single_allowed_project_is_the_default(self):
        self.assertEqual(resolve_jira_target({"summary": "file the stories"}, ["ABC"]), "ABC")

    def test_no_allowlist_and_no_hint_resolves_nothing(self):
        self.assertEqual(resolve_jira_target({"summary": "file in ABC"}, []), "")


class WithJiraTargetTests(unittest.TestCase):
    def test_adds_target_and_allowlist(self):
        out = with_jira_target({"summary": "create jira in PAY"}, _defaults("ABC", "PAY"))
        self.assertEqual(out["jira_project_key"], "PAY")
        self.assertEqual(out["jira_allowed_projects"], ["ABC", "PAY"])

    def test_leaves_intake_alone_without_config_or_hint(self):
        intake = {"summary": "write a PRD"}
        self.assertIs(with_jira_target(intake, {}), intake)

    def test_materialize_puts_target_on_the_intake_task(self):
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.program_materialize import materialize_program_workflow

        client = MagicMock()
        client.create_task.side_effect = lambda payload: {"id": payload["step_name"]}
        skeleton = load_skeleton("jira_from_prd")
        with patch("ts_harness.workflows.delivery_program._live_program_defaults", return_value=_defaults("ABC", "PAY")):
            materialize_program_workflow(
                client=client,
                skeleton=skeleton,
                workflow_type="jira_from_prd",
                program_id="prog-1",
                program_phase="jira",
                tenant_id="default",
                binding_key=None,
                requester_actor_id=None,
                intake_input={"summary": "create jira in PAY"},
            )
        intake = client.create_task.call_args_list[0].args[0]
        self.assertEqual(intake["step_name"], skeleton.intake_stage_name())
        self.assertEqual(intake["input"]["jira_project_key"], "PAY")
        self.assertEqual(intake["input"]["jira_allowed_projects"], ["ABC", "PAY"])


class JiraTargetValidationTests(unittest.TestCase):
    def _issues(self, input_map: dict) -> list:
        issues: list = []
        payload = {
            "stages": [
                {"name": "intake", "allowed_external_effects": ["jira_write"], "input_map": {"a": "$input.a"}},
                {"name": "file", "depends_on": ["intake"], "allowed_external_effects": ["jira_write"], "input_map": input_map},
            ]
        }
        _validate_jira_targets(payload, issues=issues)
        return issues

    def test_warns_when_jira_stage_drops_the_target(self):
        issues = self._issues({"plan": "$prior.intake.output"})
        self.assertEqual([issue.code for issue in issues], ["jira_target_not_passed"])
        self.assertEqual(issues[0].severity, "warning")

    def test_passing_the_target_is_enough(self):
        self.assertEqual(self._issues({"jira_allowed_projects": "$input.jira_allowed_projects"}), [])
