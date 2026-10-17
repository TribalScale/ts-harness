"""Tests for continue_delivery channel tools and policy gates."""

from __future__ import annotations

import unittest

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.delegate_confirmation import (
    resolve_continue_delivery_confirmation,
    set_pending_continue_delivery,
)
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy
from ts_harness.tools.workflow_tools import propose_continue_delivery_handler


class ContinueDeliveryToolsTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.config = load_runtime_config("config/ts-harness-runtime.test.yaml")
        self.agent = self.config.get_agent("orchestrator")

    def _session(self, **metadata) -> SessionRecord:
        session = SessionRecord(
            session_id="s-continue",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata=metadata,
        )
        get_session_store().save(session)
        return session

    def test_propose_continue_delivery_sets_pending(self):
        session = self._session(
            workflow_id="wf-prd",
            source_workflow_id="wf-prd",
            workflow_type="prd_request",
            mode="workflow_linked",
            workflow_phase="approved",
        )
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            session_id=session.session_id,
            workflow_phase="approved",
            session_mode="workflow_linked",
            bridge_available=True,
        )
        decision = evaluate_tool_policy("propose_continue_delivery", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "allow")

        result = propose_continue_delivery_handler(
            {"summary": "Start Jira and downstream delivery", "target_workflow_type": "jira_from_prd"},
            ctx,
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        saved = get_session_store().get(session.session_id)
        pending = (saved.metadata or {}).get("pending_bridge") or {}
        self.assertTrue(pending)
        # Standalone (no program_id) keeps legacy bridge pending without program nodes.
        self.assertNotIn("source_node", pending)

    def test_continue_delivery_denied_without_confirmation(self):
        session = self._session(
            workflow_id="wf-prd",
            workflow_type="prd_request",
            mode="workflow_linked",
            workflow_phase="approved",
            pending_bridge={"source_workflow_id": "wf-prd", "summary": "go"},
            approved_artifact={"summary": "PRD"},
        )
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            session_id=session.session_id,
            continue_delivery_confirmed=False,
            workflow_phase="approved",
            session_mode="workflow_linked",
        )
        decision = evaluate_tool_policy("continue_delivery", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")

    def test_continue_delivery_confirmation_flow(self):
        session = self._session()
        set_pending_continue_delivery(session, source_workflow_id="wf-prd", summary="delivery")
        result = resolve_continue_delivery_confirmation(session, "yes")
        self.assertTrue(result.confirmed)


if __name__ == "__main__":
    unittest.main()
