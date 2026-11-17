import unittest

from ts_harness.agent.sessions import SessionRecord, reset_session_store, get_session_store
from ts_harness.gateway.delegate_confirmation import (
    apply_confirmation_to_session,
    clear_pending_delegate,
    resolve_delegate_confirmation,
    set_pending_bridge,
    set_pending_delegate,
    set_pending_program,
    set_pending_program_advance,
)


class DelegateConfirmationTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()

    def _session(self, pending=None) -> SessionRecord:
        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="t1",
        )
        if pending is not None:
            session.metadata = {"pending_delegate": pending}
            get_session_store().save(session)
        return session

    def test_no_pending_never_confirms(self):
        session = self._session()
        result = resolve_delegate_confirmation(session, "yes")
        self.assertFalse(result.confirmed)

    def test_viewer_reply_does_not_confirm_delegation(self):
        session = self._session({"agent_id": "qa-agent", "task": "run tests", "input": {}})
        session.metadata = {**(session.metadata or {}), "actor_email": "view@acme.com"}
        result = resolve_delegate_confirmation(session, "yes")
        self.assertFalse(result.confirmed)
        self.assertIn("confirm_delegation", result.denied)

    def test_confirm_when_pending(self):
        session = self._session(
            {"agent_id": "qa-agent", "task": "run tests", "input": {}},
        )
        result = resolve_delegate_confirmation(session, "proceed")
        self.assertTrue(result.confirmed)
        self.assertFalse(result.clear_pending)

    def test_cancel_clears_pending(self):
        session = self._session({"agent_id": "qa-agent", "task": "run tests", "input": {}})
        result = resolve_delegate_confirmation(session, "no")
        self.assertTrue(result.clear_pending)
        apply_confirmation_to_session(session, result)
        refreshed = get_session_store().get(session.session_id)
        self.assertNotIn("pending_delegate", refreshed.metadata or {})

    def test_set_pending_delegate_persists(self):
        session = self._session()
        set_pending_delegate(session, agent_id="dev-agent", task="fix bug", input_data={"ticket": "PROJ-1"})
        refreshed = get_session_store().get(session.session_id)
        pending = (refreshed.metadata or {}).get("pending_delegate")
        self.assertEqual(pending["agent_id"], "dev-agent")
        clear_pending_delegate(refreshed)

    def test_set_pending_bridge_stores_program_identity(self):
        session = self._session()
        set_pending_bridge(
            session,
            source_workflow_id="wf-prd",
            summary="create jira",
            target_workflow_type="jira_from_prd",
            program_type="software_delivery",
            source_node="prd",
            target_node="jira",
        )
        pending = get_session_store().get(session.session_id).metadata["pending_bridge"]
        self.assertEqual(pending["program_type"], "software_delivery")
        self.assertEqual(pending["source_node"], "prd")
        self.assertEqual(pending["target_node"], "jira")

    def test_set_pending_program_and_advance_store_program_type(self):
        session = self._session()
        set_pending_program(
            session,
            entry_phase="prd",
            summary="start",
            program_type="software_delivery",
        )
        set_pending_program_advance(
            session,
            program_id="prog-1",
            summary="advance",
            target_phase="jira",
            program_type="software_delivery",
            source_node="prd",
            target_node="jira",
        )
        metadata = get_session_store().get(session.session_id).metadata
        self.assertEqual(metadata["pending_program"]["program_type"], "software_delivery")
        self.assertEqual(metadata["pending_program_advance"]["source_node"], "prd")
        self.assertEqual(metadata["pending_program_advance"]["target_node"], "jira")


if __name__ == "__main__":
    unittest.main()
