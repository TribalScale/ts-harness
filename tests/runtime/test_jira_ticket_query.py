from __future__ import annotations

import unittest

from ts_harness.jira.ticket_query import (
    build_assigned_ticket_jql,
    format_ticket_list,
    group_tickets_by_epic,
    projects_from_assigned_issues,
)


def _issue(key: str, project: str, epic: str | None, summary: str, status: str) -> dict:
    fields = {
        "summary": summary,
        "status": {"name": status},
        "project": {"key": project},
    }
    if epic:
        fields["parent"] = {"key": epic, "fields": {"summary": "Checkout rewrite"}}
    return {"key": key, "fields": fields}


class TicketQueryTests(unittest.TestCase):
    def test_build_assigned_ticket_jql(self):
        jql = build_assigned_ticket_jql(
            "PROJ",
            ["To Do", "Open", "Ready for Dev", "In Progress"],
            "acct-1",
        )
        self.assertIn('assignee = "acct-1"', jql)
        self.assertIn('project = PROJ', jql)
        self.assertIn('status IN ("To Do", Open, "Ready for Dev", "In Progress")', jql)
        self.assertNotIn("statusCategory", jql)

    def test_group_and_projects(self):
        issues = [
            _issue("PROJ-184", "PROJ", "PROJ-100", "Add cart retry", "To Do"),
            _issue("PROJ-190", "PROJ", "PROJ-100", "Tax edge cases", "In Progress"),
            _issue("PAY-9", "PAY", None, "Hotfix", "Open"),
        ]
        groups = group_tickets_by_epic(issues)
        self.assertEqual(groups[0]["epic"], "PROJ-100 — Checkout rewrite")
        self.assertEqual(len(groups[0]["issues"]), 2)
        self.assertEqual(groups[1]["epic"], "No epic")
        self.assertEqual(projects_from_assigned_issues(issues), ["PROJ", "PAY"])

    def test_format_ticket_list_grouped(self):
        issues = [
            _issue("PROJ-184", "PROJ", "PROJ-100", "Add cart retry", "To Do"),
            {**_issue("PROJ-190", "PROJ", "PROJ-100", "Tax edge cases", "In Progress"), "work_order_id": "WO-005"},
        ]
        text = format_ticket_list(
            project="PROJ",
            issues=issues,
            pointer_by_key={"PROJ-184": {"work_order_id": "WO-003"}},
            blocked_keys={"PROJ-190"},
        )
        self.assertIn("Project: PROJ", text)
        self.assertIn("Epic PROJ-100", text)
        self.assertIn("PROJ-184", text)
        self.assertIn("WO-003", text)
        self.assertIn("(blocked)", text)
        self.assertIn("Reply with a ticket key", text)

    def test_format_ticket_list_example_key_comes_from_the_list(self):
        text = format_ticket_list(project="ABC", issues=[_issue("ABC-7", "ABC", None, "Fix login", "To Do")])
        self.assertIn("Reply with a ticket key (ABC-7)", text)


if __name__ == "__main__":
    unittest.main()
