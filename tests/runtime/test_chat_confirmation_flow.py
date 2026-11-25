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


class NextPhaseOfferTests(unittest.TestCase):
    SNAPSHOT = {
        "nodes": [{"id": "bug_intake"}, {"id": "work_order"}, {"id": "file_jira"}],
        "edges": [
            {"source": "bug_intake", "target": "work_order", "mode": "manual", "signals": ["draft the work order"]},
            {"source": "work_order", "target": "file_jira", "mode": "manual", "signals": ["file these tickets"]},
            {"source": "file_jira", "target": "file_jira", "mode": "manual", "signals": ["refile the work orders"]},
        ],
    }

    def test_a_finished_phase_says_what_to_reply_for_the_next(self):
        from ts_harness.workflows.program_materialize import next_phase_offer

        offer = next_phase_offer(self.SNAPSHOT, "bug_intake")
        self.assertIn("Reply **draft the work order**", offer["text"])
        self.assertEqual(offer["next_steps"][0]["target_node"], "work_order")

    def test_a_self_edge_is_not_offered(self):
        from ts_harness.workflows.program_materialize import next_phase_offer

        self.assertEqual(next_phase_offer(self.SNAPSHOT, "file_jira")["text"], "")

    def test_a_chat_approval_reply_carries_the_offer(self):
        from ts_harness.gateway.channel_confirmations import format_workflow_gate_response_ack

        ack = format_workflow_gate_response_ack(
            {"status": "approved", "phase": {"status": "phase_completed", "offer_text": "Reply **draft the work order** to start work order."}}
        )
        self.assertEqual(ack, "Approved. Reply **draft the work order** to start work order.")


class RevisionTests(unittest.TestCase):
    def test_sending_feedback_says_the_revision_will_be_posted(self):
        from ts_harness.gateway.channel_confirmations import format_workflow_gate_response_ack

        self.assertIn("I'll post the revised version", format_workflow_gate_response_ack({"status": "rejected"}))

    def test_a_running_redraft_reads_revising(self):
        from ts_harness.workflows.phase_activity import summarize_workflow

        summary = summarize_workflow({
            "id": "wf", "status": "running", "workflow_type": "bug_work_order",
            "tasks": [
                {"id": "a", "step_name": "draft_work_order", "agent_id": "pm-agent", "status": "succeeded", "created_at": "1"},
                {"id": "b", "step_name": "approve_work_order", "agent_id": "orchestrator", "status": "cancelled", "created_at": "2"},
                {"id": "c", "step_name": "draft_work_order", "agent_id": "pm-agent", "status": "running", "created_at": "3",
                 "input": {"comments": "fix the field names"}},
            ],
        })
        self.assertTrue(summary["state"].startswith("Revising"), summary["state"])

    def test_a_revision_is_marked_when_presented(self):
        from ts_harness.workflows.hooks.present_artifact_for_review import _is_revision

        self.assertTrue(_is_revision({"input": {"comments": "x"}}))
        self.assertFalse(_is_revision({"input": {}}))


class GateReplyKindTests(unittest.TestCase):
    def test_a_repo_pick_lists_the_allowlisted_repos(self):
        from unittest.mock import patch

        from ts_harness.gateway.admin_bff import gate_reply_kind

        with patch("ts_harness.workflows.repo_pick.allowlisted_repos", return_value=[{"id": "org/web", "default_branch": "dev"}]):
            self.assertEqual(gate_reply_kind("pick_repo", "p1"), {"kind": "repo", "options": [{"id": "org/web", "default_branch": "dev"}]})
        self.assertEqual(gate_reply_kind("await_ready", "p1"), {"kind": "answer"})
        self.assertIsNone(gate_reply_kind("approve_bug", "p1"))
