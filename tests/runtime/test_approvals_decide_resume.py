"""Deciding a workflow tool approval resumes the parked stage."""

from __future__ import annotations

import unittest
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app
from ts_harness.tools import approvals as approvals_mod


class ApprovalDecideResumeTests(unittest.TestCase):
    def setUp(self):
        self.queue = approvals_mod.ApprovalQueue()
        self.memory = mock.Mock()
        auth = mock.Mock(actor_id="admin-1", roles=["admin"], tenant_id="default")
        self.patches = [
            mock.patch.object(approvals_mod, "_approval_queue", self.queue),
            mock.patch.object(asgi_app, "get_approval_queue", return_value=self.queue),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            mock.patch.object(asgi_app, "get_runtime", return_value=mock.Mock()),
            mock.patch.object(asgi_app, "_auth", return_value=auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "_require_admin", return_value=None),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def _request(self, **link):
        return self.queue.request(
            tenant_id="default", agent_id="pm-agent", tool_name="exec", args={}, reason="r", **link
        )

    def _decide(self, req, approved):
        return self.client.post(f"/admin/approvals/{req.approval_id}/decide", json={"approved": approved})

    def test_approve_resumes_linked_task(self):
        req = self._request(workflow_id="wf-1", task_id="task-1")
        body = self._decide(req, True).json()
        self.memory.resume_tool_approval.assert_called_once_with("task-1", req.approval_id, actor="admin-1")
        self.assertEqual(body["resume"], {"status": "resumed"})

    def test_reject_also_resumes_so_the_agent_can_finish(self):
        req = self._request(workflow_id="wf-1", task_id="task-1")
        self._decide(req, False)
        self.memory.resume_tool_approval.assert_called_once()

    def test_unlinked_approval_does_not_touch_memory(self):
        req = self._request()
        body = self._decide(req, True).json()
        self.memory.resume_tool_approval.assert_not_called()
        self.assertNotIn("resume", body)

    def test_already_decided_approval_returns_conflict(self):
        req = self._request(workflow_id="wf-1", task_id="task-1")
        self._decide(req, True)
        response = self._decide(req, False)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.queue.get(req.approval_id).status, "approved")
        self.memory.resume_tool_approval.assert_called_once()

    def test_resume_failure_is_reported_not_raised(self):
        self.memory.resume_tool_approval.side_effect = RuntimeError("not awaiting")
        req = self._request(workflow_id="wf-1", task_id="task-1")
        response = self._decide(req, True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["resume"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
