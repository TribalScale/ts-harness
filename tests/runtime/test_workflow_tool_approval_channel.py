"""A Slack thread reply decides a workflow stage parked on a tool approval."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from ts_harness.agent.sessions import SessionRecord
from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.channel_confirmations import (
    execute_workflow_tool_approval_reply,
    format_workflow_tool_approval_ack,
    should_intercept_workflow_tool_approval_reply,
)
from ts_harness.tools import approvals as approvals_mod
from ts_harness.tools.approvals import ApprovalQueue


def _config(role: str):
    config = load_runtime_config(
        Path(__file__).resolve().parents[2] / "config" / "ts-harness-runtime.test.yaml"
    )
    config.identity.roles = {"ada@example.com": [role]}
    return config


def _parked(approval_id: str, **extra) -> dict:
    return {
        "id": "task-1",
        "step_name": "publish",
        "status": "blocked",
        "error": f"awaiting_tool_approval:{approval_id}",
        **extra,
    }


class InterceptTests(unittest.TestCase):
    def test_approve_or_reject_reply_is_intercepted_while_a_stage_is_parked(self):
        metadata = {"workflow_id": "wf-1"}
        tasks = [{"id": "task-0", "status": "succeeded"}, _parked("ap-1")]
        for text in ("approve", "yes", "reject", "no"):
            with self.subTest(text=text):
                self.assertTrue(should_intercept_workflow_tool_approval_reply(metadata, text, tasks=tasks))

    def test_other_messages_and_unparked_workflows_pass_through(self):
        metadata = {"workflow_id": "wf-1"}
        self.assertFalse(
            should_intercept_workflow_tool_approval_reply(metadata, "what's the status?", tasks=[_parked("ap-1")])
        )
        self.assertFalse(
            should_intercept_workflow_tool_approval_reply(
                metadata, "approve", tasks=[{"id": "t", "status": "blocked", "error": "boom"}]
            )
        )
        self.assertFalse(should_intercept_workflow_tool_approval_reply({}, "approve", tasks=[_parked("ap-1")]))


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.queue = ApprovalQueue()
        self.memory = mock.Mock()
        self.patch = mock.patch.object(approvals_mod, "_approval_queue", self.queue)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _request(self, workflow_id: str = "wf-1"):
        return self.queue.request(
            tenant_id="default",
            agent_id="marketing-agent",
            tool_name="google_workspace_docs_append",
            args={"document_id": "d1", "content": "post"},
            reason="approval required",
            workflow_id=workflow_id,
            task_id="task-1",
        )

    def _run(self, req, text: str, role: str = "approver") -> dict:
        session = SessionRecord(
            session_id="s1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"workflow_id": "wf-1", "slack_user_id": "U12345678", "actor_email": "ada@example.com"},
        )
        return execute_workflow_tool_approval_reply(
            session,
            user_text=text,
            actor_id="slack:U12345678",
            channel="slack",
            auth=AuthContext(actor_id="slack:U12345678", tenant_id="default", auth_provider="slack"),
            actor_email="ada@example.com",
            config=_config(role),
            tasks=[_parked(req.approval_id)],
            memory=self.memory,
        )

    def test_approver_approves_and_the_stage_resumes(self):
        req = self._request()
        result = self._run(req, "approve")
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["tool_name"], "google_workspace_docs_append")
        self.assertEqual(result["step_name"], "publish")
        self.assertEqual(self.queue.get(req.approval_id).status, "approved")
        self.memory.resume_tool_approval.assert_called_once_with(
            "task-1", req.approval_id, actor="slack:U12345678"
        )

    def test_reject_denies_and_still_resumes_so_the_agent_can_finish(self):
        req = self._request()
        result = self._run(req, "no")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.queue.get(req.approval_id).status, "denied")
        self.memory.resume_tool_approval.assert_called_once()

    def test_member_cannot_decide(self):
        req = self._request()
        result = self._run(req, "approve", role="member")
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.queue.get(req.approval_id).status, "pending")
        self.memory.resume_tool_approval.assert_not_called()

    def test_approval_for_another_workflow_is_not_decided(self):
        req = self._request(workflow_id="wf-other")
        result = self._run(req, "approve")
        self.assertIn("error", result)
        self.assertEqual(self.queue.get(req.approval_id).status, "pending")
        self.memory.resume_tool_approval.assert_not_called()

    def test_already_decided_approval_is_reported(self):
        req = self._request()
        self.queue.decide(req.approval_id, approved=True, actor_id="admin")
        result = self._run(req, "reject")
        self.assertEqual(result["status"], "already_decided")
        self.assertEqual(self.queue.get(req.approval_id).status, "approved")


class AckTests(unittest.TestCase):
    def test_messages(self):
        base = {"tool_name": "google_workspace_docs_append", "step_name": "publish"}
        self.assertEqual(
            format_workflow_tool_approval_ack({**base, "status": "approved", "resume": {"status": "resumed"}}),
            "Approved `google_workspace_docs_append`. Resuming step `publish`.",
        )
        self.assertEqual(
            format_workflow_tool_approval_ack({**base, "status": "rejected", "resume": {"status": "resumed"}}),
            "Rejected `google_workspace_docs_append`. Step `publish` will continue without it.",
        )
        self.assertEqual(
            format_workflow_tool_approval_ack({**base, "status": "approved", "resume": {"status": "failed", "error": "x"}}),
            "Approved `google_workspace_docs_append`, but step `publish` did not resume: x",
        )
        self.assertEqual(
            format_workflow_tool_approval_ack({"status": "denied", "error": "missing permission approve_gate"}),
            "You need the approve_gate permission to decide this action.",
        )
        self.assertEqual(
            format_workflow_tool_approval_ack({**base, "status": "already_decided", "decision": "approved"}),
            "`google_workspace_docs_append` was already approved.",
        )


if __name__ == "__main__":
    unittest.main()
