"""Filing goes to the program's project, and only the filed tickets are read back and assigned."""

from __future__ import annotations

import unittest
from unittest import mock

from ts_harness.jira.assign_filed import assign_filed_tickets
from ts_harness.jira.target_project import resolve_jira_target, with_jira_target
from ts_harness.workflows.program_adapters import _extract_file_jira_outputs, filed_ticket_keys

SUMMARY = ("I filed the epic PROJ-38 in PROJ with 3 Stories under it, PROJ-39 to PROJ-41. The epic "
           "description covers invariants AD-1 to AD-6 and DoD-1 to DoD-10.")
STRUCTURED = {"summary": SUMMARY, "artifacts": {"jira_epic_key": "PROJ-38", "jira_story_keys": ["PROJ-39", "PROJ-40", "PROJ-41"]}}


def _tasks(output):
    return [
        {"step_name": "choose_epic", "status": "succeeded", "input": {"assignee_account_id": "acct-9", "jira_project_key": "PROJ"}},
        {"step_name": "file_tickets", "status": "succeeded", "updated_at": "2", "input": {"jira_project_key": "PROJ"}, "output": output},
    ]


class FiledKeysTests(unittest.TestCase):
    def test_keys_under_artifacts_are_used(self):
        self.assertEqual(filed_ticket_keys(STRUCTURED, project="PROJ"), ("PROJ-38", ["PROJ-39", "PROJ-40", "PROJ-41"]))

    def test_keys_read_from_the_summary_stay_in_the_filed_project(self):
        epic, keys = filed_ticket_keys({"summary": SUMMARY}, project="PROJ")
        self.assertEqual(epic, "PROJ-38")
        self.assertTrue(keys and all(key.startswith("PROJ-") for key in keys), keys)

    def test_assigning_touches_only_the_filed_tickets(self):
        for output in (STRUCTURED, {"summary": SUMMARY}):
            memory = mock.Mock()
            memory.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": _tasks(output)}
            jira = mock.Mock()
            out = assign_filed_tickets({"workflow_id": "wf"}, {}, memory=memory, jira_factory=lambda: jira)
            touched = [call.args[0] for call in jira.update_issue.call_args_list]
            self.assertTrue(touched and all(key.startswith("PROJ-") for key in touched), touched)
            self.assertIn("PROJ-39", out["assigned"])

    def test_assigning_never_leaves_the_allowed_projects(self):
        tasks = _tasks({"summary": "Filed.", "jira_story_keys": ["PROJ-39", "OTHER-1"]})
        tasks[0]["input"]["jira_allowed_projects"] = ["PROJ"]
        tasks[1]["input"] = {}
        tasks[0]["input"].pop("jira_project_key")
        memory = mock.Mock()
        memory.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": tasks}
        jira = mock.Mock()
        assign_filed_tickets({"workflow_id": "wf"}, {}, memory=memory, jira_factory=lambda: jira)
        self.assertEqual([call.args[0] for call in jira.update_issue.call_args_list], ["PROJ-39"])

    def test_program_artifacts_record_only_the_filed_tickets(self):
        patch = _extract_file_jira_outputs(workflow_type="file_work_orders_as_jira", tasks=_tasks(STRUCTURED))
        self.assertEqual(patch, {"jira_epic_key": "PROJ-38", "jira_story_keys": ["PROJ-39", "PROJ-40", "PROJ-41"]})


class ProjectTargetTests(unittest.TestCase):
    def test_the_projects_jira_key_beats_the_single_allowed_fallback(self):
        self.assertEqual(resolve_jira_target({"summary": "file the work orders"}, ["OPS"], project_key="pay"), "PAY")
        # Naming an allowed project in the request still wins.
        self.assertEqual(resolve_jira_target({"summary": "file in OPS"}, ["OPS"], project_key="PAY"), "OPS")
        self.assertEqual(resolve_jira_target({"summary": "file"}, ["OPS"]), "OPS")

    def test_materialize_reads_the_programs_project(self):
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.program_materialize import materialize_program_workflow

        client = mock.MagicMock()
        client.create_task.side_effect = lambda payload: {"id": payload["step_name"]}
        # Programs name their project by key; get_project only takes a UUID.
        client.get_program.return_value = {"program": {"id": "prog-1", "project_id": "PAY", "tenant_id": "default"}}
        client.get_project.side_effect = RuntimeError("project_id must be a UUID")
        client.list_projects.return_value = {"projects": [{"id": "0b6c", "key": "PAY", "jira_project_key": "PAY"}]}
        skeleton = load_skeleton("file_work_orders_as_jira")
        with mock.patch("ts_harness.workflows.delivery_program._live_program_defaults",
                        return_value={"jira_filing": {"allowed_projects": ["OPS", "PAY"]}}):
            materialize_program_workflow(
                client=client, skeleton=skeleton, workflow_type="file_work_orders_as_jira", program_id="prog-1",
                program_phase="file_jira", tenant_id="default", binding_key=None, requester_actor_id=None,
                intake_input={"summary": "file the work orders"},
            )
        intake = client.create_task.call_args_list[0].args[0]
        self.assertEqual(intake["input"]["jira_project_key"], "PAY")
        self.assertEqual(with_jira_target({"summary": "x"}, {"jira_filing": {"allowed_projects": ["OPS"]}})["jira_project_key"], "OPS")


if __name__ == "__main__":
    unittest.main()
