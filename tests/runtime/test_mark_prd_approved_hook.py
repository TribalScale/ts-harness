"""Tests for mark_prd_approved hook."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.workflows.hooks import mark_prd_approved
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


class MarkPrdApprovedHookTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()

    @patch("ts_harness.workflows.hooks.notify_bound_channel.handle")
    def test_marks_session_approved_without_downstream_tasks(self, mock_notify):
        mock_notify.return_value = {"status": "notified"}
        session = SessionRecord(
            session_id="s-prd",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"workflow_id": "wf-prd", "mode": "prd_linked", "prd_phase": "awaiting_approval"},
        )
        get_session_store().save(session)

        skeleton = load_skeleton("prd_request")
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"source_channel": "slack:C1:123.456"},
            "tasks": [
                {
                    "id": "draft-1",
                    "step_name": "draft_prd",
                    "status": "succeeded",
                    "output": {"summary": "PRD body"},
                    "created_at": "2026-01-01",
                },
            ],
        }
        ctx = HookContext(
            workflow_id="wf-prd",
            workflow_type="prd_request",
            tenant_id="default",
            task={"id": "gate-1", "step_name": "approve_prd"},
            stage=skeleton.stage("approve_prd"),
            skeleton=skeleton,
            actor="orchestrator",
            memory_client=client,
            extras={"session": session},
        )

        result = mark_prd_approved.handle(ctx)
        self.assertEqual(result["status"], "artifact_approved")
        saved = get_session_store().get("s-prd")
        self.assertEqual(saved.metadata.get("workflow_phase"), "approved")
        self.assertEqual(saved.metadata.get("approved_artifact"), {"summary": "PRD body"})
        client.create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
