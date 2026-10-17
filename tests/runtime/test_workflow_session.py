"""Tests for generic workflow session phase synchronization."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.workflow_session import (
    set_workflow_phase_for_workflow,
    sync_workflow_session_for_workflow,
    sync_workflow_session_phase,
)


class WorkflowSessionPhaseTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.skeleton = load_skeleton("prd_request")

    @patch("ts_harness.workflows.engine.get_workflow_engine")
    def test_sync_sets_awaiting_approval_from_engine(self, mock_engine_factory):
        mock_engine = MagicMock()
        mock_engine.get_workflow_phase.return_value = "awaiting_approval"
        mock_engine_factory.return_value = mock_engine

        session = SessionRecord(
            session_id="s-phase",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "mode": "workflow_linked",
                "workflow_type": "prd_request",
                "workflow_id": "wf-prd",
                "workflow_phase": "drafting",
            },
        )
        get_session_store().save(session)

        phase = sync_workflow_session_phase(session, self.skeleton)
        self.assertEqual(phase, "awaiting_approval")
        saved = get_session_store().get("s-phase")
        self.assertEqual(saved.metadata.get("workflow_phase"), "awaiting_approval")

    @patch("ts_harness.workflows.engine.get_workflow_engine")
    def test_sync_for_workflow_updates_linked_session(self, mock_engine_factory):
        mock_engine = MagicMock()
        mock_engine.get_workflow_phase.return_value = "awaiting_approval"
        mock_engine_factory.return_value = mock_engine

        session = SessionRecord(
            session_id="s-linked",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"mode": "workflow_linked", "workflow_type": "prd_request", "workflow_id": "wf-1"},
        )
        get_session_store().save(session)

        sync_workflow_session_for_workflow("wf-1", self.skeleton, tenant_id="default")
        saved = get_session_store().get("s-linked")
        self.assertEqual(saved.metadata.get("workflow_phase"), "awaiting_approval")

    def test_set_workflow_phase_for_workflow_pushes_immediately(self):
        session = SessionRecord(
            session_id="s-push",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "mode": "workflow_linked",
                "workflow_type": "prd_request",
                "workflow_id": "wf-push",
                "workflow_phase": "drafting",
            },
        )
        get_session_store().save(session)

        set_workflow_phase_for_workflow("wf-push", "awaiting_approval", self.skeleton, tenant_id="default")
        saved = get_session_store().get("s-push")
        self.assertEqual(saved.metadata.get("workflow_phase"), "awaiting_approval")


if __name__ == "__main__":
    unittest.main()
