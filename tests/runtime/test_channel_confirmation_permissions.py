"""Channel yes/no replies honor the same permissions as the console."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from ts_harness.agent.sessions import SessionRecord
from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.channel_confirmations import execute_confirmed_tool_approval
from ts_harness.gateway.delegate_confirmation import (
    resolve_bridge_confirmation,
    resolve_program_advance_confirmation,
    resolve_program_confirmation,
    resolve_workflow_confirmation,
    set_pending_bridge,
    set_pending_program,
    set_pending_program_advance,
    set_pending_workflow,
)
from ts_harness.tools.approvals import ApprovalQueue


def _config(role: str):
    config = load_runtime_config(
        Path(__file__).resolve().parents[2] / "config" / "ts-harness-runtime.test.yaml"
    )
    config.identity.roles = {"ada@example.com": [role]}
    return config


def _slack_session(**metadata: object) -> SessionRecord:
    session = SessionRecord(
        session_id="s1",
        routing_key="rk",
        tenant_id="default",
        agent_id="orchestrator",
        source="slack",
        metadata={"slack_user_id": "U12345678", "actor_email": "ada@example.com", **metadata},
    )
    return session


def _confirm_kwargs(role: str) -> dict:
    return {
        "channel": "slack",
        "auth": AuthContext(actor_id="slack:webhook", tenant_id="default", auth_provider="slack"),
        "actor_email": "ada@example.com",
        "config": _config(role),
    }


class ChannelStartPermissionTests(unittest.TestCase):
    def test_member_cannot_confirm_workflow_program_advance_or_bridge(self):
        cases = (
            (set_pending_workflow, resolve_workflow_confirmation, {"workflow_type": "prd_request", "summary": "draft"}),
            (set_pending_program, resolve_program_confirmation, {"entry_phase": "prd", "summary": "draft"}),
            (
                set_pending_program_advance,
                resolve_program_advance_confirmation,
                {"program_id": "p1", "summary": "next"},
            ),
            (
                set_pending_bridge,
                resolve_bridge_confirmation,
                {"source_workflow_id": "wf-1", "summary": "continue"},
            ),
        )
        for setter, resolver, kwargs in cases:
            with self.subTest(resolver=resolver.__name__):
                session = _slack_session()
                setter(session, **kwargs)
                result = resolver(session, "yes", **_confirm_kwargs("member"))
                self.assertFalse(result.confirmed)
                self.assertEqual(result.denied, "missing permission start_program")

    def test_requester_can_confirm_workflow_start(self):
        session = _slack_session()
        set_pending_workflow(session, workflow_type="prd_request", summary="draft")
        result = resolve_workflow_confirmation(session, "yes", **_confirm_kwargs("requester"))
        self.assertTrue(result.confirmed)
        self.assertEqual(result.denied, "")

    def test_web_requester_confirms_and_viewer_is_denied(self):
        session = _slack_session()
        session.source = "web"
        set_pending_workflow(session, workflow_type="prd_request", summary="draft")
        allowed = resolve_workflow_confirmation(
            session,
            "yes",
            channel="web",
            auth=AuthContext(actor_id="ada", tenant_id="default", roles=["requester"], auth_provider="oidc"),
            config=_config("member"),
        )
        self.assertTrue(allowed.confirmed)
        denied = resolve_workflow_confirmation(
            session,
            "yes",
            channel="web",
            auth=AuthContext(actor_id="ada", tenant_id="default", roles=["viewer"], auth_provider="oidc"),
            config=_config("member"),
        )
        self.assertFalse(denied.confirmed)
        self.assertEqual(denied.denied, "missing permission start_program")

    def test_program_confirmation_uses_the_node_permission(self):
        session = _slack_session()
        set_pending_program(
            session,
            entry_phase="implement",
            summary="ship",
            program_type="software_delivery",
        )
        with mock.patch(
            "ts_harness.gateway.program_definitions_bff.program_action_permission",
            return_value="start_delivery",
        ):
            denied = resolve_program_confirmation(session, "yes", **_confirm_kwargs("requester"))
            self.assertFalse(denied.confirmed)
            self.assertEqual(denied.denied, "missing permission start_delivery")
            allowed_kwargs = _confirm_kwargs("admin")
            allowed_kwargs["config"].identity.role_bundles = {"delivery_lead": ["view", "start_delivery"]}
            allowed = resolve_program_confirmation(session, "yes", **allowed_kwargs)
        self.assertTrue(allowed.confirmed)
        self.assertEqual(allowed.denied, "")


class ChannelToolApprovalPermissionTests(unittest.TestCase):
    def _run(self, role: str) -> dict:
        queue = ApprovalQueue()
        args = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
        req = queue.request(
            tenant_id="default",
            agent_id="comms-agent",
            tool_name="google_workspace_gmail_send",
            args=args,
            reason="approval required",
        )
        session = _slack_session(
            pending_tool_approval={"approval_id": req.approval_id, "tool_name": "google_workspace_gmail_send", "args": args}
        )
        registry = mock.Mock()
        registry.dispatch.return_value = {"sent": True}
        from ts_harness.tools import approvals as approvals_mod

        original = approvals_mod._approval_queue
        approvals_mod._approval_queue = queue
        try:
            result = execute_confirmed_tool_approval(
                session,
                tenant_id="default",
                agent_id="comms-agent",
                source="slack",
                user_text="yes",
                actor_id="slack:U12345678",
                registry=registry,
                config=_config(role),
                actor_email="ada@example.com",
                channel="slack",
                auth=AuthContext(actor_id="slack:webhook", tenant_id="default", auth_provider="slack"),
            )
        finally:
            approvals_mod._approval_queue = original
        result["_queue_status"] = queue.get(req.approval_id).status
        result["_dispatched"] = registry.dispatch.called
        return result

    def test_member_cannot_approve_a_tool(self):
        result = self._run("member")
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["_queue_status"], "pending")
        self.assertFalse(result["_dispatched"])

    def test_approver_can_approve_a_tool(self):
        result = self._run("approver")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["_dispatched"])


if __name__ == "__main__":
    unittest.main()
