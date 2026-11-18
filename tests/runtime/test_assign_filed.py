import unittest
from unittest import mock

from ts_harness.jira.assign_filed import assign_filed_tickets


def _memory(tasks):
    memory = mock.Mock()
    memory.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": tasks}
    return memory


CHOOSE = {"step_name": "choose_epic", "status": "succeeded", "input": {"assignee_account_id": "acct-9"}}
FILED = {"step_name": "file_tickets", "status": "succeeded", "updated_at": "2",
         "output": {"summary": "I filed epic THD-34 with THD-35, THD-36 and THD-37."}}


class AssignFiledTicketsTests(unittest.TestCase):
    def test_assigns_every_filed_story_to_the_requester(self):
        jira = mock.Mock()
        out = assign_filed_tickets({"workflow_id": "wf"}, {}, memory=_memory([CHOOSE, FILED]), jira_factory=lambda: jira)
        self.assertEqual(out["assigned"], ["THD-35", "THD-36", "THD-37"])
        jira.update_issue.assert_any_call("THD-35", fields={"assignee": {"accountId": "acct-9"}})
        self.assertEqual(jira.update_issue.call_count, 3)
        self.assertEqual(out["summary"], "Assigned THD-35, THD-36, THD-37 to you.")

    def test_reports_a_ticket_jira_refused(self):
        jira = mock.Mock()
        jira.update_issue.side_effect = [None, RuntimeError("403 not assignable"), None]
        out = assign_filed_tickets({"workflow_id": "wf"}, {}, memory=_memory([CHOOSE, FILED]), jira_factory=lambda: jira)
        self.assertEqual(out["assigned"], ["THD-35", "THD-37"])
        self.assertIn("Could not assign: THD-36 (403 not assignable)", out["summary"])

    def test_without_an_assignee_it_does_nothing_and_posts_nothing(self):
        jira = mock.Mock()
        choose = {**CHOOSE, "input": {}}
        self.assertEqual(assign_filed_tickets({"workflow_id": "wf"}, {}, memory=_memory([choose, FILED]), jira_factory=lambda: jira), {})
        jira.update_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
