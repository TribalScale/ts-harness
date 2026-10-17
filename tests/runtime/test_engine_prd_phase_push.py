"""Engine pushes prd_phase at task transition boundaries."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.workflows.engine import WorkflowEngine
from ts_harness.workflows.hooks import build_default_registry


class EnginePrdPhasePushTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.client = MagicMock()
        self.engine = WorkflowEngine(client=self.client, registry=build_default_registry())
        session = SessionRecord(
            session_id="s-engine",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"mode": "workflow_linked", "workflow_type": "prd_request", "workflow_id": "wf-1", "workflow_phase": "drafting"},
        )
        get_session_store().save(session)

    def test_draft_prd_success_sets_awaiting_approval(self):
        self.client.get_workflow.return_value = {
            "workflow": {"workflow_type": "prd_request"},
            "tasks": [],
        }
        self.engine.on_task_transition(
            {
                "id": "draft-1",
                "workflow_id": "wf-1",
                "workflow_type": "prd_request",
                "tenant_id": "default",
                "step_name": "draft_prd",
                "status": "succeeded",
            },
            "succeeded",
        )
        saved = get_session_store().get("s-engine")
        self.assertEqual(saved.metadata.get("workflow_phase"), "awaiting_approval")


if __name__ == "__main__":
    unittest.main()
