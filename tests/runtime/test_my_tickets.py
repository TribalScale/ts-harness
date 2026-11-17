"""Show my tickets: the signed-in person's Jira account, their ticket list, and what they pick."""

import unittest
from types import SimpleNamespace

from ts_harness.jira.identity import UNMAPPED, jira_account_for_email, lookup_jira_account
from ts_harness.jira.prepare_ticket_queue import prepare_ticket_queue
from ts_harness.jira.ticket_choice import ticket_choice_from_reply
from ts_harness.workflows.system_steps import StepBlocked


class _Jira:
    def __init__(self, users):
        self.users = users
        self.paths = []

    def request(self, method, path, body=None):
        self.paths.append(path)
        return self.users


def _issue(key, epic=None):
    fields = {"summary": f"{key} summary", "status": {"name": "To Do"}, "issuelinks": []}
    if epic:
        fields["parent"] = {"key": epic, "fields": {"summary": "Discussions"}}
    return {"key": key, "fields": fields}


class _Memory:
    def __init__(self, summary, *, email="pm@example.com", actor="console-subject-123"):
        self.summary, self.email, self.actor = summary, email, actor

    def get_workflow(self, workflow_id):
        return {
            "workflow": {"id": workflow_id, "program_id": "prog-1", "requester_actor_id": self.actor},
            "tasks": [{"step_name": "prepare_ticket_queue", "input": {"summary": self.summary}}],
        }

    def get_program_status(self, program_id):
        return {"artifacts": {"requester_email": self.email} if self.email else {}}

    def get_program(self, program_id):
        return {"program": {"program_definition_snapshot": {"config": {}}}}


RUNTIME = SimpleNamespace(identity=SimpleNamespace(slack_user_emails={}, jira_account_ids={}))


class IdentityTests(unittest.TestCase):
    def test_settings_mapping_wins_then_jira_search_by_exact_email(self):
        jira = _Jira([{"accountId": "acct-9", "emailAddress": "PM@example.com", "accountType": "atlassian", "active": True}])
        self.assertEqual(jira_account_for_email("pm@example.com", {"pm@example.com": "acct-1"}, jira), "acct-1")
        self.assertEqual(jira.paths, [])
        self.assertEqual(jira_account_for_email("pm@example.com", {}, jira), "acct-9")
        self.assertIn("query=pm%40example.com", jira.paths[0])

    def test_hidden_emails_take_a_single_human_match_only(self):
        one = _Jira([{"accountId": "acct-2", "accountType": "atlassian"}])
        self.assertEqual(lookup_jira_account(one, "pm@example.com"), "acct-2")
        two = _Jira([{"accountId": "a"}, {"accountId": "b"}])
        self.assertEqual(lookup_jira_account(two, "pm@example.com"), UNMAPPED)
        bot = _Jira([{"accountId": "app", "accountType": "app"}])
        self.assertEqual(lookup_jira_account(bot, "pm@example.com"), UNMAPPED)
        self.assertEqual(lookup_jira_account(one, "not-an-email"), UNMAPPED)


class MyTicketsTests(unittest.TestCase):
    def _run(self, summary, issues, *, email="pm@example.com", users=None):
        seen = {}

        def search(jql, *, max_results, fields):
            seen["jql"] = jql
            return issues

        jira = _Jira(users if users is not None else [{"accountId": "acct-9", "emailAddress": email}])
        out = prepare_ticket_queue(
            {"workflow_id": "wf-1", "input": {"summary": summary}}, {}, memory=_Memory(summary, email=email),
            search=search, runtime=RUNTIME, jira=jira,
        )
        return out, seen

    def test_a_console_user_sees_their_tickets_across_projects(self):
        out, seen = self._run("show my tickets", [_issue("THD-33", "THD-30"), _issue("ABC-2")])
        self.assertIn('assignee = "acct-9"', seen["jql"])
        self.assertNotIn("project =", seen["jql"])
        self.assertEqual(out["tickets"], ["THD-33", "ABC-2"])
        self.assertEqual(set(out["queues"]), {"THD", "ABC"})
        self.assertIn("Project: THD", out["summary"])
        self.assertIn("THD-33", out["summary"])
        self.assertIn("Reply with a ticket key (THD-33)", out["summary"])
        self.assertEqual(out["jira_project_key"], "")

    def test_without_an_account_it_says_how_to_continue(self):
        with self.assertRaises(StepBlocked) as raised:
            self._run("show my tickets", [], users=[])
        self.assertIn("`pm@example.com`", str(raised.exception))
        self.assertIn("implement THD-33", str(raised.exception))

    def test_no_tickets_is_said_plainly(self):
        out, _ = self._run("show my tickets", [])
        self.assertIn("No tickets are assigned to you", out["summary"])


class TicketChoiceTests(unittest.TestCase):
    LISTING = {
        "tickets": ["THD-33", "THD-34"],
        "queues": {"THD": {"jira_issue_keys": ["THD-34", "THD-33"], "queue_exclusions": []}},
    }

    def test_a_key_is_the_ticket_to_implement(self):
        self.assertEqual(ticket_choice_from_reply("thd-33 please", self.LISTING), {"jira_issue_key": "THD-33", "jira_project_key": "THD"})

    def test_all_project_freezes_its_queue_in_order(self):
        choice = ticket_choice_from_reply("all THD", self.LISTING)
        self.assertEqual(choice["jira_issue_keys"], ["THD-34", "THD-33"])
        self.assertEqual(choice["jira_issue_key"], "THD-34")
        self.assertEqual(ticket_choice_from_reply("all of them", self.LISTING)["jira_project_key"], "THD")

    def test_yes_takes_the_only_ticket(self):
        self.assertEqual(ticket_choice_from_reply("yes", {"tickets": ["THD-33"]})["jira_issue_key"], "THD-33")
        self.assertEqual(ticket_choice_from_reply("yes", self.LISTING), {})


if __name__ == "__main__":
    unittest.main()


class PickGateTests(unittest.TestCase):
    def test_replying_with_a_key_picks_that_ticket(self):
        from unittest.mock import MagicMock

        from ts_harness.workflows.hooks import approve_gate
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.registry import HookContext

        skeleton = load_skeleton("pick_assigned_jira")
        self.assertEqual([stage.name for stage in skeleton.stages], ["prepare_ticket_queue", "pick_ticket"])
        client = MagicMock()
        client.approve_task.return_value = {"status": "succeeded"}
        tasks = [
            {"id": "q-1", "step_name": "prepare_ticket_queue", "status": "succeeded",
             "output": {"queue": False, "tickets": ["THD-33", "THD-34"], "queues": {}}},
            {"id": "pick-1", "step_name": "pick_ticket", "status": "pending", "input": {"jira_allowed_projects": ["THD"]}},
        ]
        client.get_workflow.return_value = {"workflow": {"id": "wf-1"}, "tasks": tasks}
        ctx = HookContext(
            workflow_id="wf-1", workflow_type="pick_assigned_jira", tenant_id="default", task=tasks[1],
            stage=skeleton.stage("pick_ticket"), skeleton=skeleton, actor="orchestrator", memory_client=client,
            extras={"decision": {}, "reply_text": "THD-34"},
        )
        approve_gate.handle(ctx)
        output = client.approve_task.call_args.kwargs["output"]
        self.assertEqual(output["jira_issue_key"], "THD-34")
        self.assertNotIn("jira_epic_key", output)
