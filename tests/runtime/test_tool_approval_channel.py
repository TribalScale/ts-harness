"""Tests for Slack tool approval resume via channel confirmations."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from ts_harness.config.schema import load_runtime_config
from ts_harness.agent.sessions import SessionRecord
from ts_harness.gateway.channel_confirmations import (
    execute_confirmed_tool_approval,
    should_intercept_tool_approval_reply,
)
from ts_harness.tools.approvals import ApprovalQueue


class ToolApprovalChannelTests(unittest.TestCase):
    def test_should_intercept_when_pending_and_approve(self):
        metadata = {
            "pending_tool_approval": {
                "approval_id": "abc",
                "tool_name": "google_workspace_gmail_send",
                "args": {"to": "a@example.com", "subject": "Hi", "body": "Hello"},
            }
        }
        self.assertTrue(should_intercept_tool_approval_reply(metadata, "yes"))

    def test_execute_confirmed_tool_approval_dispatches(self):
        queue = ApprovalQueue()
        args = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
        req = queue.request(
            tenant_id="default",
            agent_id="comms-agent",
            tool_name="google_workspace_gmail_send",
            args=args,
            reason="approval required",
        )
        session = SessionRecord(
            session_id="sess-1",
            routing_key="slack:default:sess-1",
            tenant_id="default",
            source="slack",
            agent_id="comms-agent",
            metadata={
                "pending_tool_approval": {
                    "approval_id": req.approval_id,
                    "tool_name": "google_workspace_gmail_send",
                    "args": args,
                }
            },
        )
        registry = mock.Mock()
        registry.dispatch.return_value = {"sent": True, "id": "msg-1"}
        config = load_runtime_config(
            Path(__file__).resolve().parents[2] / "config" / "ts-harness-runtime.test.yaml"
        )

        from ts_harness.tools import approvals as approvals_mod

        original = approvals_mod._approval_queue
        try:
            approvals_mod._approval_queue = queue
            result = execute_confirmed_tool_approval(
                session,
                tenant_id="default",
                agent_id="comms-agent",
                source="slack",
                user_text="yes",
                actor_id="slack:U1",
                registry=registry,
                config=config,
                actor_email="user@corp.com",
            )
        finally:
            approvals_mod._approval_queue = original

        self.assertEqual(result.get("status"), "succeeded")
        registry.dispatch.assert_called_once()
        self.assertIsNone((session.metadata or {}).get("pending_tool_approval"))


if __name__ == "__main__":
    unittest.main()
