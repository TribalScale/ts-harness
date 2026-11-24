"""Confirming a proposal in the chat: approvals with extra words, the reply that says how to
confirm stays in the thread, and the assistant knows a waiting proposal starts on a yes."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from ts_harness.gateway.confirmation_classifier import classify_confirmation_deterministic
from ts_harness.gateway.server import _build_channel_system_context, _waiting_proposal, _with_final_reply


class ApprovalWordingTests(unittest.TestCase):
    def decision(self, text):
        return classify_confirmation_deterministic(text, context="workflow_start").decision

    def test_an_approval_with_only_more_approval_is_a_yes(self):
        for text in ("yes, start it", "yes please", "ok go ahead", "confirm, thanks!", "go ahead and start it", "yes start it now"):
            self.assertEqual(self.decision(text), "approve", text)

    def test_an_approval_with_anything_else_is_not_a_plain_yes(self):
        for text in ("yes but change the repo", "yes, use PROJ", "yesterday was fine", "okay so what next?"):
            self.assertEqual(self.decision(text), "none", text)

    def test_a_no_with_words_is_still_a_no(self):
        self.assertEqual(self.decision("no, start it later"), "reject")


class FinalReplyTests(unittest.TestCase):
    def test_a_turn_ending_on_a_proposal_keeps_its_reply(self):
        messages = [
            {"role": "assistant", "content": "This is a bug. I'm proposing that now.", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "{}"},
        ]
        result = SimpleNamespace(status="succeeded", output={"summary": "Proposed. Reply **yes** to start, or **no** to cancel."})
        out = _with_final_reply(messages, result)
        self.assertEqual(out[-1], {"role": "assistant", "content": "Proposed. Reply **yes** to start, or **no** to cancel."})

    def test_a_reply_already_last_is_not_repeated(self):
        messages = [{"role": "assistant", "content": "Done."}]
        self.assertEqual(_with_final_reply(messages, SimpleNamespace(status="succeeded", output={"summary": "Done."})), messages)


class WaitingProposalTests(unittest.TestCase):
    def test_a_waiting_program_is_named_for_the_assistant(self):
        session = SimpleNamespace(metadata={"pending_program": {"program_type": "bug_to_jira_work_orders"}})
        text = _build_channel_system_context(session, delegation_confirmed=False, workflow_confirmed=False)
        self.assertIn("bug to jira work orders program is waiting", text)
        self.assertIn("Reply **yes** to start it", text)
        self.assertIn("Never say it cannot be started", text)

    def test_nothing_is_said_once_it_was_confirmed(self):
        self.assertEqual(_waiting_proposal({"pending_program": {"program_type": "x"}}, confirmed=True), "")
        self.assertEqual(_waiting_proposal({}, confirmed=False), "")


if __name__ == "__main__":
    unittest.main()
