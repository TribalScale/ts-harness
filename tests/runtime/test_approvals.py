"""Basic tests for the approval queue and approval_required policy decisions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ts_harness.config.schema import load_runtime_config, parse_runtime_config
from unittest import mock

from ts_harness.tools.approvals import (
    ApprovalAlreadyDecided,
    ApprovalQueue,
    args_fingerprint,
    decide_and_resume,
    scan_command,
)
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy


REPO_ROOT = Path(__file__).resolve().parents[2]


class ApprovalQueueTests(unittest.TestCase):
    def test_create_pending_approval_request(self):
        queue = ApprovalQueue()
        req = queue.request(
            tenant_id="default",
            agent_id="orchestrator",
            tool_name="exec",
            args={"command": "ls -la"},
            reason="tool requires approval",
        )
        self.assertEqual(req.status, "pending")
        self.assertEqual(req.tool_name, "exec")
        self.assertEqual(req.tenant_id, "default")

    def test_decided_for_task_returns_unconsumed_decisions_for_that_task(self):
        queue = ApprovalQueue()
        mine = queue.request(tenant_id="t", agent_id="pm-agent", tool_name="google_workspace_docs_append",
                             args={"document_id": "d"}, reason="r", workflow_id="wf-1", task_id="task-1")
        other = queue.request(tenant_id="t", agent_id="pm-agent", tool_name="exec",
                              args={}, reason="r", workflow_id="wf-1", task_id="task-2")
        pending = queue.request(tenant_id="t", agent_id="pm-agent", tool_name="exec",
                                args={}, reason="r", workflow_id="wf-1", task_id="task-1")
        queue.decide(mine.approval_id, approved=True)
        queue.decide(other.approval_id, approved=True)

        self.assertEqual([a.approval_id for a in queue.decided_for_task("wf-1", "task-1")], [mine.approval_id])
        queue.consume(mine.approval_id)
        self.assertEqual(queue.decided_for_task("wf-1", "task-1"), [])
        self.assertEqual(pending.status, "pending")

    def test_task_link_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.jsonl"
            queue = ApprovalQueue(path)
            req = queue.request(tenant_id="t", agent_id="a", tool_name="exec", args={}, reason="r",
                                workflow_id="wf-1", task_id="task-1")
            reloaded = ApprovalQueue(path).get(req.approval_id)
            self.assertEqual((reloaded.workflow_id, reloaded.task_id), ("wf-1", "task-1"))

    def test_approve_request(self):
        queue = ApprovalQueue()
        req = queue.request(
            tenant_id="default",
            agent_id="orchestrator",
            tool_name="write_file",
            args={"path": "/tmp/test.txt", "content": "hello"},
            reason="write requires approval",
        )
        decided = queue.decide(req.approval_id, approved=True, actor_id="admin")
        self.assertEqual(decided.status, "approved")
        self.assertEqual(decided.decided_by_actor_id, "admin")

    def test_deny_request(self):
        queue = ApprovalQueue()
        req = queue.request(
            tenant_id="default",
            agent_id="dev-agent",
            tool_name="exec",
            args={"command": "rm -rf /tmp"},
            reason="exec requires approval",
        )
        decided = queue.decide(req.approval_id, approved=False, actor_id="reviewer", roles=["admin"])
        self.assertEqual(decided.status, "denied")
        self.assertEqual(decided.decided_by_roles, ["admin"])

    def test_get_request_by_id(self):
        queue = ApprovalQueue()
        req = queue.request(
            tenant_id="default",
            agent_id="orchestrator",
            tool_name="delegate_task",
            args={},
            reason="delegation requires approval",
        )
        fetched = queue.get(req.approval_id)
        self.assertEqual(fetched.approval_id, req.approval_id)

    def test_queue_persists_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.jsonl"
            q1 = ApprovalQueue(path=path)
            req = q1.request(
                tenant_id="default",
                agent_id="orchestrator",
                tool_name="exec",
                args={},
                reason="test",
            )
            q1.decide(req.approval_id, approved=True, actor_id="admin")

            q2 = ApprovalQueue(path=path)
            recovered = q2.get(req.approval_id)
            self.assertEqual(recovered.status, "approved")

    def test_queue_isolates_tenants(self):
        queue = ApprovalQueue()
        req_a = queue.request(tenant_id="tenant-a", agent_id="orchestrator", tool_name="exec", args={}, reason="r")
        req_b = queue.request(tenant_id="tenant-b", agent_id="orchestrator", tool_name="exec", args={}, reason="r")
        self.assertNotEqual(req_a.approval_id, req_b.approval_id)
        self.assertEqual(queue.get(req_a.approval_id).tenant_id, "tenant-a")
        self.assertEqual(queue.get(req_b.approval_id).tenant_id, "tenant-b")

    def test_args_fingerprint_stable(self):
        args = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
        self.assertEqual(args_fingerprint(args), args_fingerprint(dict(args)))

    def test_is_approved_and_matches(self):
        queue = ApprovalQueue()
        args = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
        req = queue.request(
            tenant_id="default",
            agent_id="comms-agent",
            tool_name="google_workspace_gmail_send",
            args=args,
            reason="send requires approval",
        )
        self.assertFalse(
            queue.is_approved_and_matches(
                req.approval_id,
                tool_name="google_workspace_gmail_send",
                args=args,
                tenant_id="default",
                agent_id="comms-agent",
            )
        )
        queue.decide(req.approval_id, approved=True, actor_id="admin")
        self.assertTrue(
            queue.is_approved_and_matches(
                req.approval_id,
                tool_name="google_workspace_gmail_send",
                args=args,
                tenant_id="default",
                agent_id="comms-agent",
            )
        )

    def test_consume_invalidates_reuse(self):
        queue = ApprovalQueue()
        args = {"document_id": "doc1", "content": "text"}
        req = queue.request(
            tenant_id="default",
            agent_id="pm-agent",
            tool_name="google_workspace_docs_append",
            args=args,
            reason="append requires approval",
        )
        queue.decide(req.approval_id, approved=True, actor_id="admin")
        queue.consume(req.approval_id)
        self.assertFalse(
            queue.is_approved_and_matches(
                req.approval_id,
                tool_name="google_workspace_docs_append",
                args=args,
                tenant_id="default",
                agent_id="pm-agent",
            )
        )


class DecideAndResumeTests(unittest.TestCase):
    def setUp(self):
        self.queue = ApprovalQueue()
        self.memory = mock.Mock()

    def _request(self, **link):
        return self.queue.request(
            tenant_id="t", agent_id="pm-agent", tool_name="exec", args={}, reason="r", **link
        )

    def test_decision_on_a_linked_request_resumes_the_task(self):
        req = self._request(workflow_id="wf-1", task_id="task-1")
        approval, resume = decide_and_resume(
            self.queue, self.memory, req.approval_id, approved=True, actor_id="slack:U1"
        )
        self.assertEqual(approval.status, "approved")
        self.assertEqual(approval.decided_by_actor_id, "slack:U1")
        self.memory.resume_tool_approval.assert_called_once_with("task-1", req.approval_id, actor="slack:U1")
        self.assertEqual(resume, {"status": "resumed"})

    def test_unlinked_request_is_decided_without_a_resume(self):
        req = self._request()
        approval, resume = decide_and_resume(self.queue, self.memory, req.approval_id, approved=False, actor_id="a")
        self.assertEqual(approval.status, "denied")
        self.assertIsNone(resume)
        self.memory.resume_tool_approval.assert_not_called()

    def test_resume_failure_keeps_the_decision(self):
        self.memory.resume_tool_approval.side_effect = RuntimeError("not awaiting")
        req = self._request(workflow_id="wf-1", task_id="task-1")
        approval, resume = decide_and_resume(self.queue, self.memory, req.approval_id, approved=True, actor_id="a")
        self.assertEqual(approval.status, "approved")
        self.assertEqual(resume, {"status": "failed", "error": "not awaiting"})

    def test_second_decision_is_refused(self):
        req = self._request(workflow_id="wf-1", task_id="task-1")
        decide_and_resume(self.queue, self.memory, req.approval_id, approved=True, actor_id="a")
        with self.assertRaises(ApprovalAlreadyDecided):
            decide_and_resume(self.queue, self.memory, req.approval_id, approved=False, actor_id="b")
        self.assertEqual(self.queue.get(req.approval_id).status, "approved")
        self.memory.resume_tool_approval.assert_called_once()


class CommandScanTests(unittest.TestCase):
    def test_allows_safe_command(self):
        ok, reason = scan_command("ls -la /tmp")
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_blocks_rm_rf_root(self):
        ok, reason = scan_command("rm -rf /")
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_blocks_mkfs(self):
        ok, reason = scan_command("mkfs.ext4 /dev/sda1")
        self.assertFalse(ok)

    def test_blocks_sudo(self):
        ok, reason = scan_command("sudo apt-get install vim")
        self.assertFalse(ok)

    def test_blocks_env_file_access(self):
        ok, reason = scan_command("cat .env")
        self.assertFalse(ok)

    def test_blocks_aws_secret_access(self):
        ok, reason = scan_command("echo $AWS_SECRET_ACCESS_KEY")
        self.assertFalse(ok)


class ApprovalRequiredPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")

    def test_exec_requires_approval(self):
        agent = self.config.get_agent("dev-agent")
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="workflow")
        # exec is in approval_required_tools in the test config
        decision = evaluate_tool_policy("exec", agent, self.config, ctx)
        self.assertIn(decision.decision, {"approval_required", "deny"})

    def test_write_file_requires_approval(self):
        agent = self.config.get_agent("dev-agent")
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="workflow")
        decision = evaluate_tool_policy("write_file", agent, self.config, ctx)
        self.assertIn(decision.decision, {"approval_required", "deny"})

    def test_read_file_does_not_require_approval(self):
        agent = self.config.get_agent("dev-agent")
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="workflow")
        decision = evaluate_tool_policy("read_file", agent, self.config, ctx)
        self.assertEqual(decision.decision, "allow")

    def test_approval_id_bypasses_policy(self):
        data = {
            "gateway": {"token": "x"},
            "tenants": [{"id": "default", "display_name": "Default", "enabled_connectors": ["google-workspace"]}],
            "agents": {
                "list": [
                    {
                        "id": "comms-agent",
                        "default": True,
                        "workspace": "/w",
                        "model": "openai/gpt-4o-mini",
                        "tools": {"allow": ["google_workspace_gmail_send"], "toolsets": ["google-workspace"]},
                    }
                ]
            },
            "policy": {"approval_required_tools": ["google_workspace_gmail_send"]},
        }
        config = parse_runtime_config(data)
        agent = config.get_agent("comms-agent")
        queue = ApprovalQueue()
        args = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
        req = queue.request(
            tenant_id="default",
            agent_id="comms-agent",
            tool_name="google_workspace_gmail_send",
            args=args,
            reason="send requires approval",
        )
        queue.decide(req.approval_id, approved=True, actor_id="admin")
        from ts_harness.tools import approvals as approvals_mod

        original = approvals_mod._approval_queue
        try:
            approvals_mod._approval_queue = queue
            ctx = ToolPolicyContext(
                tenant_id="default",
                agent_id="comms-agent",
                source="slack",
                approval_id=req.approval_id,
            )
            decision = evaluate_tool_policy(
                "google_workspace_gmail_send",
                agent,
                config,
                ctx,
                tool_args=args,
            )
            self.assertEqual(decision.decision, "allow")
            self.assertEqual(decision.policy_id, "approval_consumed")
        finally:
            approvals_mod._approval_queue = original

    def test_approval_policy_id_is_approval_required(self):
        data = {
            "gateway": {"token": "x"},
            "agents": {
                "list": [
                    {
                        "id": "orchestrator",
                        "default": True,
                        "workspace": "/w",
                        "model": "openai/gpt-4o-mini",
                        "tools": {"allow": ["exec"]},
                    }
                ]
            },
            "policy": {"approval_required_tools": ["exec"]},
        }
        config = parse_runtime_config(data)
        agent = config.get_agent("orchestrator")
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
        decision = evaluate_tool_policy("exec", agent, config, ctx)
        self.assertEqual(decision.decision, "approval_required")
        self.assertEqual(decision.policy_id, "approval_required")


if __name__ == "__main__":
    unittest.main()
