"""Steps after a revision's chain (close_ticket) follow the replacement merge wait, not the cancelled one."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.hooks.revise_on_reject import repoint_dangling_tasks
from ts_harness.workflows.materialize import load_skeleton


class DanglingDependencyTests(unittest.TestCase):
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk", return_value=["close-2"])
    def test_close_ticket_waits_on_the_new_merge_wait(self, bulk):
        tasks = [
            {"id": "merge-1", "step_name": "await_human_merge", "status": "cancelled", "created_at": "1"},
            {"id": "merge-2", "step_name": "await_human_merge", "status": "succeeded", "created_at": "3"},
            {"id": "close-1", "step_name": "close_ticket", "status": "pending", "created_at": "2", "agent_id": "orchestrator",
             "depends_on": ["merge-1"], "input": {"jira_issue_key": "PROJ-2"}},
            {"id": "other", "step_name": "notify", "status": "pending", "created_at": "2", "depends_on": ["merge-2"]},
        ]
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {}, "tasks": tasks}
        created = repoint_dangling_tasks(memory, workflow_id="wf", workflow_type="implement_jira_ticket", tenant_id="default",
                                         skeleton=load_skeleton("implement_jira_ticket"), actor="runner")
        self.assertEqual(created, ["close-2"])
        memory.cancel_task.assert_called_once_with("close-1", actor="runner", reason="re-pointed after revision")
        body = bulk.call_args.kwargs["tasks"][0]
        self.assertEqual((body["step_name"], body["depends_on"], body["input"]), ("close_ticket", ["merge-2"], {"jira_issue_key": "PROJ-2"}))

    def test_nothing_changes_when_no_step_waits_on_a_cancelled_one(self):
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {}, "tasks": [
            {"id": "a", "step_name": "x", "status": "succeeded", "created_at": "1"},
            {"id": "b", "step_name": "y", "status": "pending", "created_at": "2", "depends_on": ["a"]}]}
        self.assertEqual(repoint_dangling_tasks(memory, workflow_id="wf", workflow_type="implement_jira_ticket", tenant_id="default",
                                                skeleton=load_skeleton("implement_jira_ticket"), actor="runner"), [])
        memory.cancel_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
