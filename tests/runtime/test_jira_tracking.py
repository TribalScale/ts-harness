import unittest
from unittest import mock

from ts_harness.jira.tracking import close_ticket, move_ticket, tracking_settings

ON = {"enabled": True, "start_transition": "In Progress", "done_transition": "Done"}


class TrackingTests(unittest.TestCase):
    def test_settings_default_and_follow_program_defaults(self):
        self.assertEqual(tracking_settings({}), ON)
        custom = tracking_settings({"jira_tracking": {"done_transition": "Ready for QA", "enabled": False}})
        self.assertEqual((custom["done_transition"], custom["enabled"]), ("Ready for QA", False))

    def test_move_reports_a_missing_transition_without_failing(self):
        jira = mock.Mock()
        jira.transition.return_value = True
        self.assertEqual(move_ticket("THD-35", "start", jira=jira, settings=ON), "")
        jira.transition.assert_called_once_with("THD-35", "In Progress")
        jira.transition.return_value = False
        self.assertIn("no transition named Done", move_ticket("THD-35", "done", jira=jira, settings=ON))
        jira.transition.side_effect = RuntimeError("401")
        self.assertIn("could not move THD-35", move_ticket("THD-35", "done", jira=jira, settings=ON))
        self.assertEqual(move_ticket("THD-35", "done", jira=mock.Mock(), settings={**ON, "enabled": False}), "")

    def test_close_ticket_moves_it_to_done_and_links_the_pr(self):
        memory = mock.Mock()
        memory.get_workflow.return_value = {
            "workflow": {"id": "wf", "input": {"jira_issue_key": "THD-35"}},
            "tasks": [
                {"step_name": "prepare_ticket_delivery", "created_at": "1", "output": {"jira_issue_key": "THD-35"}},
                {"step_name": "deliver_work_order", "created_at": "2", "output": {"pr_url": "https://github.com/o/r/pull/9"}},
            ],
        }
        jira = mock.Mock()
        jira.transition.return_value = True
        with mock.patch("ts_harness.jira.tracking.tracking_settings", return_value=ON):
            out = close_ticket({"workflow_id": "wf"}, {}, memory=memory, jira=jira)
        jira.transition.assert_called_once_with("THD-35", "Done")
        jira.comment.assert_called_once_with("THD-35", "Merged: https://github.com/o/r/pull/9 (TS Harness)")
        self.assertIn("THD-35 moved to Done", out["summary"])


if __name__ == "__main__":
    unittest.main()
