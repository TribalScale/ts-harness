"""Pending chat proposals: listed, and cancelled with /cancel or from the Inbox."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.agent.sessions import SessionRecord
from ts_harness.gateway import proposals


def _session(metadata):
    return SessionRecord(session_id="s1", routing_key="slack:C1:t1", tenant_id="default", agent_id="orchestrator",
                         source="slack", metadata=dict(metadata))


PROGRAM = {"pending_program": {"entry_phase": "pick", "summary": "implement all PROJ", "program_type": "jira_to_pr",
                               "proposed_at": "2026-01-02T10:00:00Z"}}


class ProposalTests(unittest.TestCase):
    def test_the_cancel_command(self):
        for text in ("/cancel", "/cancel proposal", " /CANCEL the proposals "):
            self.assertTrue(proposals.is_cancel_command(text), text)
        for text in ("cancel", "/cancel PROJ-7", "no", "/stop PROJ-7"):
            self.assertFalse(proposals.is_cancel_command(text), text)

    def test_pending_proposals_are_described(self):
        found = proposals.pending_proposals({**PROGRAM, "pending_delegate": {"agent_id": "pm-agent", "task": "Draft the PRD"}})
        self.assertEqual([p["key"] for p in found], ["pending_program", "pending_delegate"])
        self.assertEqual(found[0]["description"], "Start a program `jira_to_pr`: implement all PROJ")
        self.assertEqual(found[0]["proposed_at"], "2026-01-02T10:00:00Z")
        self.assertEqual(proposals.pending_proposals({"program_id": "p1"}), [])

    def test_cancel_drops_them_and_says_what(self):
        session = _session({**PROGRAM, "program_id": "p1"})
        store = MagicMock()
        cancelled = proposals.cancel_proposals(session, store=store)
        self.assertEqual([c["key"] for c in cancelled], ["pending_program"])
        self.assertNotIn("pending_program", session.metadata)
        self.assertEqual(session.metadata["program_id"], "p1")  # the conversation's link stays
        store.save.assert_called_once_with(session)
        self.assertIn("Cancelled the pending proposal:\n- Start a program `jira_to_pr`", proposals.cancel_ack(cancelled))
        self.assertEqual(proposals.cancel_ack([]), "Nothing is waiting for a yes or no in this conversation.")

    def test_the_inbox_lists_every_conversation_with_one_waiting(self):
        store = MagicMock()
        store.list_for_tenant.return_value = [_session(PROGRAM), _session({})]
        rows = proposals.tenant_proposals("default", store=store)
        self.assertEqual([(r["session_id"], r["key"], r["source"]) for r in rows], [("s1", "pending_program", "slack")])


if __name__ == "__main__":
    unittest.main()
