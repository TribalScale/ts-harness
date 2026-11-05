"""Ordering rules for the assigned-ticket queue."""

from __future__ import annotations

import unittest

from ts_harness.jira.ticket_queue import (
    QUEUE_CAP,
    order_ticket_queue,
    queue_is_active,
    remaining_queue_keys,
    wants_ticket_queue,
)


def _blocked_by(key: str, status: str = "To Do") -> dict:
    return {
        "type": {"inward": "is blocked by", "outward": "blocks"},
        "inwardIssue": {"key": key, "fields": {"status": {"name": status}}},
    }


def _issue(key: str, *blockers: tuple[str, str]) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": key,
            "status": {"name": "To Do"},
            "issuelinks": [_blocked_by(blocker, status) for blocker, status in blockers],
        },
    }


class OrderTicketQueueTests(unittest.TestCase):
    def test_blocker_comes_before_the_blocked_ticket(self):
        ordered = order_ticket_queue(
            [_issue("PROJ-2", ("PROJ-1", "In Progress")), _issue("PROJ-1")]
        )
        self.assertEqual(ordered["ordered"], ["PROJ-1", "PROJ-2"])
        self.assertEqual(ordered["excluded"], [])

    def test_outside_blocker_excludes_the_ticket_and_names_the_key(self):
        ordered = order_ticket_queue([_issue("PROJ-2", ("PROJ-9", "Open")), _issue("PROJ-1")])
        self.assertEqual(ordered["ordered"], ["PROJ-1"])
        self.assertEqual(
            ordered["excluded"],
            [{"key": "PROJ-2", "reason": "blocked by PROJ-9"}],
        )

    def test_done_blocker_is_ignored(self):
        ordered = order_ticket_queue([_issue("PROJ-2", ("PROJ-9", "Done"))])
        self.assertEqual(ordered["ordered"], ["PROJ-2"])
        self.assertEqual(ordered["excluded"], [])

    def test_cycle_is_excluded(self):
        ordered = order_ticket_queue(
            [
                _issue("PROJ-2", ("PROJ-1", "To Do")),
                _issue("PROJ-1", ("PROJ-2", "To Do")),
                _issue("PROJ-3"),
            ]
        )
        self.assertEqual(ordered["ordered"], ["PROJ-3"])
        self.assertEqual(
            ordered["excluded"],
            [
                {"key": "PROJ-1", "reason": "cycle"},
                {"key": "PROJ-2", "reason": "cycle"},
            ],
        )

    def test_fifty_first_key_is_excluded(self):
        issues = [_issue(f"PROJ-{index:03d}") for index in range(1, QUEUE_CAP + 2)]
        ordered = order_ticket_queue(issues)
        self.assertEqual(len(ordered["ordered"]), QUEUE_CAP)
        self.assertEqual(ordered["ordered"][0], "PROJ-001")
        self.assertEqual(
            ordered["excluded"],
            [{"key": f"PROJ-{QUEUE_CAP + 1:03d}", "reason": "queue cap 50"}],
        )

    def test_ties_break_by_issue_key(self):
        ordered = order_ticket_queue([_issue("PROJ-2"), _issue("PROJ-10"), _issue("PROJ-1")])
        self.assertEqual(ordered["ordered"], ["PROJ-1", "PROJ-10", "PROJ-2"])

    def test_queue_phrases(self):
        self.assertTrue(wants_ticket_queue("implement all my tickets"))
        self.assertTrue(wants_ticket_queue("please implement the rest"))
        self.assertFalse(wants_ticket_queue("implement PROJ-184"))
        self.assertFalse(wants_ticket_queue("show my tickets"))

    def test_active_queue_keeps_unfinished_keys(self):
        artifacts = {
            "jira_issue_keys": ["PROJ-1", "PROJ-2"],
            "completed_jira_issue_keys": ["PROJ-1"],
        }
        self.assertEqual(remaining_queue_keys(artifacts), ["PROJ-2"])
        self.assertTrue(queue_is_active(artifacts))
        self.assertFalse(queue_is_active({"jira_issue_key": "PROJ-1"}))


class PrepareTicketQueueTests(unittest.TestCase):
    def _memory(self, summary, project="PROJ", actor="slack:U12345678"):
        class Memory:
            def get_workflow(self, workflow_id):
                return {
                    "workflow": {
                        "id": workflow_id,
                        "program_id": "prog-1",
                        "requester_actor_id": actor,
                    },
                    "tasks": [
                        {
                            "step_name": "list_projects",
                            "status": "succeeded",
                            "input": {"summary": summary},
                            "output": {"jira_project_key": project},
                        }
                    ],
                }

            def get_program(self, program_id):
                return {"program": {"program_definition_snapshot": {"config": {}}}}

        return Memory()

    def _runtime(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            identity=SimpleNamespace(
                slack_user_emails={"U12345678": "dev@example.com"},
                jira_account_ids={"dev@example.com": "acct-1"},
            )
        )

    def test_named_key_does_not_search(self):
        from ts_harness.jira.prepare_ticket_queue import prepare_ticket_queue

        def search(*_args, **_kwargs):
            raise AssertionError("search")

        result = prepare_ticket_queue(
            {"workflow_id": "wf-1", "input": {"summary": "implement PROJ-184"}},
            {},
            memory=self._memory("implement PROJ-184"),
            search=search,
            runtime=self._runtime(),
        )
        self.assertEqual(result, {"queue": False})

    def test_unmapped_account_blocks_without_a_list(self):
        from ts_harness.workflows.system_steps import StepBlocked

        from ts_harness.jira.prepare_ticket_queue import prepare_ticket_queue

        with self.assertRaises(StepBlocked) as raised:
            prepare_ticket_queue(
                {"workflow_id": "wf-1", "input": {"summary": "implement all"}},
                {},
                memory=self._memory("implement all", actor="slack:U00000000"),
                search=lambda *_args, **_kwargs: [],
                runtime=self._runtime(),
            )
        self.assertIn("ticket key", str(raised.exception).lower())
        self.assertEqual(raised.exception.output.get("jira_issue_keys", []), [])

    def test_queue_request_freezes_blocker_order(self):
        from ts_harness.jira.prepare_ticket_queue import prepare_ticket_queue

        seen = {}

        def search(jql, *, max_results, fields):
            seen["jql"] = jql
            seen["max_results"] = max_results
            seen["fields"] = fields
            return [
                _issue("PROJ-2", ("PROJ-1", "To Do")),
                _issue("PROJ-1"),
            ]

        result = prepare_ticket_queue(
            {"workflow_id": "wf-1", "input": {"summary": "implement the rest"}},
            {},
            memory=self._memory("implement the rest"),
            search=search,
            runtime=self._runtime(),
        )
        self.assertEqual(result["jira_issue_keys"], ["PROJ-1", "PROJ-2"])
        self.assertEqual(result["jira_issue_key"], "PROJ-1")
        self.assertIn("assignee", seen["jql"])
        self.assertEqual(seen["max_results"], 100)
        self.assertIn("issuelinks", seen["fields"])
        self.assertIn("PROJ-1", result["summary"])


if __name__ == "__main__":
    unittest.main()
