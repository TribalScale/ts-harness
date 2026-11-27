"""Hand-written Jira tickets: context, the readiness decision, questions and answers."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from ts_harness.jira import ticket_readiness as tr
from ts_harness.jira.pointer_block import parse_pointer_block
from ts_harness.workflows.system_steps import StepBlocked
from ts_harness.workflows.work_order_waves import HUMAN_DECISION_KEY

WORK_ORDER = "## Summary\nShow the count.\n\n## Acceptance Tests\n- Given 3 comments, When I open the list, Then the row shows 3."


class FakeJira:
    def __init__(self, comments=()):
        self._comments = list(comments)
        self.posted: list[tuple[str, str]] = []

    def comments(self, key):
        return self._comments

    def comment(self, key, text):
        self.posted.append((key, text))

    def get_issue(self, key):
        return {"key": key, "fields": {"summary": "The epic", "description": "Epic goal: faster planning."}}


def _issue():
    return {"key": "PROJ-7", "fields": {
        "summary": "Show the comment count", "parent": {"key": "PROJ-1"},
        "issuelinks": [{"type": {"inward": "is blocked by", "outward": "blocks"},
                        "inwardIssue": {"key": "PROJ-5", "fields": {"summary": "Count API", "status": {"name": "Done"}}}}],
    }}


def _model(result, prompts=None):
    def call(prompt):
        if prompts is not None:
            prompts.append(prompt)
        return json.dumps(result)
    return call


def _check(result, *, jira=None, guidance="", autonomy="autonomous", prompts=None):
    memory = mock.Mock()
    github = mock.Mock()
    github.request.return_value = {"items": [{"title": "PROJ-5 count API", "state": "closed", "html_url": "https://github.com/org/web/pull/2"}]}
    github.get_tree.return_value = {"tree": [{"type": "blob", "path": "src/List.tsx"}, {"type": "blob", "path": "node_modules/x.js"}]}
    task = {"id": "prep-1", "input": {"jira_issue_key": "PROJ-7"}}
    out = tr.check_ready(
        memory=memory, task=task, task_input={"human_guidance": guidance} if guidance else {}, jira=jira or FakeJira(),
        key="PROJ-7", issue=_issue(), summary="Show the comment count", description="Show how many comments a row has.",
        repo_url="https://github.com/org/web", base_branch="dev", github=github, model_call=_model(result, prompts),
        autonomy=autonomy,
    )
    return out, memory


class PolicyTests(unittest.TestCase):
    def test_ready_or_minor_assumptions_go(self):
        self.assertEqual(tr.decide({"status": "ready", "work_order": WORK_ORDER}, autonomy="autonomous", go_ahead=False), "go")
        minor = {"status": "assumptions", "work_order": WORK_ORDER, "assumptions": [{"text": "put it in List.tsx", "kind": "minor"}]}
        self.assertEqual(tr.decide(minor, autonomy="autonomous", go_ahead=False), "go")

    def test_behaviour_assumptions_and_unready_tickets_wait(self):
        behaviour = {"status": "assumptions", "work_order": WORK_ORDER, "assumptions": [{"text": "hide zero counts", "kind": "behaviour"}]}
        self.assertEqual(tr.decide(behaviour, autonomy="autonomous", go_ahead=False), "wait")
        self.assertEqual(tr.decide({"status": "not_ready", "questions": ["Which list?"]}, autonomy="autonomous", go_ahead=False), "wait")

    def test_go_ahead_accepts_the_assumptions(self):
        behaviour = {"status": "assumptions", "work_order": WORK_ORDER, "assumptions": [{"text": "hide zero counts", "kind": "behaviour"}]}
        self.assertEqual(tr.decide(behaviour, autonomy="autonomous", go_ahead=True), "go")

    def test_conservative_projects_confirm_any_assumption(self):
        minor = {"status": "assumptions", "work_order": WORK_ORDER, "assumptions": [{"text": "naming", "kind": "minor"}]}
        self.assertEqual(tr.decide(minor, autonomy="conservative", go_ahead=False), "wait")
        self.assertEqual(tr.decide({"status": "ready", "work_order": WORK_ORDER}, autonomy="conservative", go_ahead=False), "go")

    def test_answers_and_go_ahead_are_read_from_the_retry_guidance(self):
        guidance = "The previous run stopped: PROJ-7 isn't ready...\n\nGuidance from jira-reply:\nAlex Doe: go ahead, zero counts hidden is fine"
        self.assertEqual(tr.person_answers(guidance), "Alex Doe: go ahead, zero counts hidden is fine")
        self.assertTrue(tr.said_go_ahead(tr.person_answers(guidance)))
        self.assertFalse(tr.said_go_ahead("Alex Doe: the list on the Orders page"))

    def test_only_tickets_without_a_planning_pointer_are_checked(self):
        self.assertTrue(tr.hand_written(parse_pointer_block("Do the thing.\n\nRepo: org/web@dev")))
        self.assertFalse(tr.hand_written(parse_pointer_block("Program: abc-123\nWork order: WO-004\nRepo: org/web@dev")))


class CheckReadyTests(unittest.TestCase):
    def test_a_ready_ticket_builds_from_the_work_order_posted_on_the_ticket(self):
        jira = FakeJira(comments=[{"id": "1", "author": {"displayName": "Sam"}, "created": "2026-11-27T09:00:00.000+0000",
                                   "body": "Only the Orders list."}])
        prompts: list[str] = []
        out, _memory = _check({"status": "ready", "work_order": WORK_ORDER, "reason": "clear"}, jira=jira, prompts=prompts)
        self.assertTrue(out["body"].startswith("# PROJ-7 Show the comment count"))
        self.assertIn("## Acceptance Tests", out["body"])
        self.assertIn("## Original ticket", out["body"])
        self.assertTrue(jira.posted[0][1].startswith("[TS Harness] Building PROJ-7"))
        for context in ("Only the Orders list.", "PROJ-1: The epic", "is blocked by PROJ-5: Count API", "pull/2", "src/List.tsx"):
            self.assertIn(context, prompts[0])
        self.assertNotIn("node_modules", prompts[0])

    def test_an_unready_ticket_asks_and_waits_without_building(self):
        jira = FakeJira()
        with self.assertRaises(StepBlocked) as ctx:
            _check({"status": "not_ready", "questions": ["Which list shows the count?"], "reason": "the list isn't named"}, jira=jira)
        self.assertIn("Which list shows the count?", str(ctx.exception))
        self.assertIn("Which list shows the count?", jira.posted[0][1])
        self.assertIn('Reply "go ahead"', jira.posted[0][1])

    def test_the_wait_is_marked_for_the_inbox_and_the_reply_check(self):
        memory = mock.Mock()
        github = mock.Mock()
        github.request.return_value = {"items": []}
        github.get_tree.return_value = {"tree": []}
        with self.assertRaises(StepBlocked):
            tr.check_ready(memory=memory, task={"id": "prep-1", "input": {}}, task_input={}, jira=FakeJira(), key="PROJ-7",
                           issue=_issue(), summary="s", description="d", repo_url="https://github.com/org/web",
                           base_branch="dev", github=github, autonomy="autonomous",
                           model_call=_model({"status": "assumptions", "work_order": WORK_ORDER,
                                              "assumptions": [{"text": "zero counts are hidden", "kind": "behaviour"}]}))
        mark = memory.patch_task_input.call_args.args[1][HUMAN_DECISION_KEY]
        self.assertEqual((mark["reason"], mark["jira_issue_key"]), ("ticket_not_ready", "PROJ-7"))

    def test_go_ahead_after_the_questions_builds_with_the_assumptions_listed(self):
        result = {"status": "assumptions", "work_order": WORK_ORDER, "assumptions": [{"text": "zero counts are hidden", "kind": "behaviour"}]}
        out, _ = _check(result, guidance="The previous run stopped: ...\n\nGuidance from Sam:\ngo ahead")
        self.assertIn("## Assumptions\n- zero counts are hidden (changes behaviour or scope)", out["body"])

    def test_no_model_or_switched_off_goes_through_as_before(self):
        with mock.patch.dict("os.environ", {"TS_HARNESS_TICKET_READINESS": "off"}):
            self.assertIsNone(tr.check_ready(memory=mock.Mock(), task={}, task_input={}, jira=FakeJira(), key="K", issue={},
                                             summary="", description="", repo_url="", base_branch=""))
        with mock.patch("ts_harness.workflows.revision_guidance.diagnosis_model", return_value=None):
            self.assertIsNone(tr.check_ready(memory=mock.Mock(), task={}, task_input={}, jira=FakeJira(), key="K", issue={},
                                             summary="", description="", repo_url="", base_branch=""))


class ReplyTests(unittest.TestCase):
    def _waiting(self, asked_at):
        return {"id": "prep-1", "workflow_id": "wf-1", "input": {HUMAN_DECISION_KEY: {
            "reason": "ticket_not_ready", "asked_at": asked_at, "jira_issue_key": "PROJ-7"}}}

    def run_recheck(self, comments, asked_at="2026-11-27T15:00:00+00:00"):
        memory = mock.Mock()
        memory.list_stuck_tasks.return_value = [self._waiting(asked_at), {"id": "other", "input": {}}]
        engine = mock.Mock()
        out = tr.recheck_waiting_tickets(memory=memory, engine=engine, jira=FakeJira(comments=comments))
        return out, engine

    def test_a_reply_after_the_questions_reruns_the_step_with_it(self):
        # 10:30 in UTC-5 is 15:30 UTC: after the questions, though the text sorts before them.
        reply = {"id": "9", "author": {"displayName": "Sam"}, "created": "2026-11-27T10:30:00.000-0500", "body": "The Orders list."}
        out, engine = self.run_recheck([reply])
        self.assertEqual(out["resumed"], 1)
        args, kwargs = engine.decide_blocked_step.call_args
        self.assertEqual(args, ("wf-1", "prep-1", "retry"))
        self.assertIn("Sam: The Orders list.", kwargs["guidance"])

    def test_older_comments_and_the_harnesss_own_are_not_replies(self):
        old = {"id": "1", "author": {"displayName": "Sam"}, "created": "2026-11-27T09:00:00.000-0500", "body": "earlier"}
        ours = {"id": "2", "author": {"displayName": "TS Harness"}, "created": "2026-11-27T16:00:00.000+0000",
                "body": "[TS Harness] PROJ-7 isn't ready to build yet"}
        out, engine = self.run_recheck([old, ours])
        self.assertEqual((out["checked"], out["resumed"]), (1, 0))
        engine.decide_blocked_step.assert_not_called()


class PrepareStepTests(unittest.TestCase):
    def test_the_ticket_step_builds_a_hand_written_ticket_from_the_checked_work_order(self):
        from tests.runtime.test_ticket_delivery import TASK, _Jira, _memory, _not_needed
        from ts_harness.jira.ticket_delivery import prepare_ticket_delivery

        calls = []

        def readiness(**kwargs):
            calls.append(kwargs["key"])
            return {"body": "# THD-33 Show the comment count\n\n## Summary\nChecked work order.\n", "note": "Built from a work order."}

        with mock.patch("ts_harness.jira.ticket_delivery.JiraClient", create=True):
            out = prepare_ticket_delivery(TASK, {}, memory=_memory(), jira_factory=lambda: _Jira(), inspect=_not_needed,
                                          ready=lambda **_: [], readiness_check=readiness)
        self.assertEqual(calls, ["THD-33"])
        self.assertIn("Checked work order.", out["work_order_body"])
        self.assertIn("Built from a work order.", out["summary"])

    def test_a_harness_filed_ticket_is_not_checked(self):
        from tests.runtime.test_ticket_delivery import TASK, _Jira, _memory, _not_needed
        from ts_harness.jira.ticket_delivery import prepare_ticket_delivery

        filed = _Jira(description="Do it.\n\nProgram: prd-prog\nWork order: WO-001\nRepo: acme/web@fe-dev")
        readiness = mock.Mock()
        prepare_ticket_delivery(TASK, {}, memory=_memory(), jira_factory=lambda: filed, inspect=_not_needed,
                                ready=lambda **_: [], readiness_check=readiness)
        readiness.assert_not_called()


if __name__ == "__main__":
    unittest.main()
