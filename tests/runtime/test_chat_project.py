"""The project picked in the chat box is kept, shown on proposals and given to the program."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.gateway import chat_project as cp
from ts_harness.gateway.channel_confirmations import format_program_proposal_message, format_workflow_proposal_message
from ts_harness.gateway.delegate_confirmation import set_pending_workflow
from ts_harness.tools.policy import ToolPolicyContext


def _memory(*, program_project=None):
    memory = mock.Mock()
    memory.list_projects.side_effect = lambda tenant_id, key="": {
        "projects": [p for p in [{"key": "ACME", "name": "Acme Scheduler"}] if not key or p["key"] == key]
    }
    memory.get_program.return_value = {"program": {"id": "p1", "project_id": program_project}}
    return memory


class ChatProjectTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()

    def _session(self, **metadata):
        session = SessionRecord(session_id="s1", routing_key="rk", tenant_id="t", agent_id="orchestrator", source="web",
                                metadata={"channel": "web", "channel_id": "web", "thread_id": "s1", **metadata})
        get_session_store().save(session)
        return session

    def test_the_pick_is_kept_and_an_unknown_key_ignored(self):
        session = self._session()
        memory = _memory()
        cp.apply_chat_project(session, {"project_key": "ACME"}, tenant_id="t", memory=memory, actor="u")
        saved = get_session_store().get("s1").metadata
        self.assertEqual(saved[cp.SESSION_KEY], {"key": "ACME", "name": "Acme Scheduler"})
        self.assertEqual(cp.project_label(saved), "Acme Scheduler (ACME)")
        cp.apply_chat_project(session, {"project_key": "NOPE"}, tenant_id="t", memory=memory, actor="u")
        cp.apply_chat_project(session, {"project_key": ""}, tenant_id="t", memory=memory, actor="u")
        self.assertEqual(get_session_store().get("s1").metadata[cp.SESSION_KEY]["key"], "ACME")
        memory.patch_program.assert_not_called()

    def test_a_linked_program_without_a_project_gets_it(self):
        session = self._session(program_id="p1")
        memory = _memory()
        note = cp.apply_chat_project(session, {"project_key": "ACME"}, tenant_id="t", memory=memory, actor="u")
        memory.patch_program.assert_called_once_with("p1", tenant_id="t", project_id="ACME", project_id_source="chat", actor="u")
        self.assertEqual(note, "Project set to Acme Scheduler (ACME).")
        other = _memory(program_project="OTHER")
        cp.apply_chat_project(self._session(program_id="p1"), {"project_key": "ACME"}, tenant_id="t", memory=other, actor="u")
        other.patch_program.assert_not_called()

    def test_proposals_show_the_project(self):
        self.assertIn("Project: Acme Scheduler (ACME).\nReply yes",
                      format_workflow_proposal_message({"workflow_type": "prd_request", "summary": "s", "project": "Acme Scheduler (ACME)"}))
        self.assertIn("Project: Acme Scheduler (ACME).\nReply **yes**",
                      format_program_proposal_message({"program_type": "x", "project": "Acme Scheduler (ACME)"}))
        self.assertNotIn("Project:", format_workflow_proposal_message({"workflow_type": "prd_request", "summary": "s"}))

    def test_starting_a_prd_gives_its_program_the_project(self):
        from ts_harness.tools.workflow_tools import propose_start_workflow_handler, start_workflow_handler

        self._session(**{cp.SESSION_KEY: {"key": "ACME", "name": "Acme Scheduler"}})
        ctx = ToolPolicyContext(tenant_id="t", agent_id="orchestrator", source="web", session_id="s1")
        proposal = propose_start_workflow_handler({"workflow_type": "prd_request", "summary": "Create a PRD"}, ctx)
        self.assertEqual(proposal["project"], "Acme Scheduler (ACME)")
        set_pending_workflow(get_session_store().get("s1"), workflow_type="prd_request", summary="Create a PRD", input_data={})
        memory = _memory()

        def start(**kwargs):
            # Promotion registers the program and links the chat to it.
            linked = get_session_store().get("s1")
            linked.metadata = {**linked.metadata, "program_id": "p1"}
            get_session_store().save(linked)
            return SimpleNamespace(workflow_id="w1", workflow_type="prd_request", first_step="gather_sources", binding_key="b", task_ids=[])

        with mock.patch("ts_harness.tools.workflow_tools.WorkflowPromotionService") as service, \
                mock.patch("ts_harness.tools.workflow_tools.get_memory_client", return_value=memory):
            service.return_value.start.side_effect = start
            result = start_workflow_handler({}, ctx)
        self.assertEqual(result["status"], "started")
        memory.patch_program.assert_called_once_with("p1", tenant_id="t", project_id="ACME", project_id_source="chat", actor="orchestrator")


if __name__ == "__main__":
    unittest.main()
