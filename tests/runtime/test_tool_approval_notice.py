"""A stage parked on a tool approval tells the bound Slack thread how to decide it."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.engine import WorkflowEngine
from ts_harness.workflows.hooks import build_default_registry


class ToolApprovalNoticeTests(unittest.TestCase):
    def test_notice_names_step_and_tool_and_how_to_decide(self):
        engine = WorkflowEngine(client=MagicMock(), registry=build_default_registry())
        task = {
            "id": "task-1",
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "tenant_id": "default",
            "step_name": "publish",
            "agent_id": "marketing-agent",
        }
        with patch.object(engine, "_maybe_notify", return_value={"status": "notified"}) as notify:
            result = engine.notify_tool_approval_waiting(task, tool_name="google_workspace_docs_append")

        self.assertEqual(result, {"status": "notified"})
        ctx, event = notify.call_args.args
        self.assertEqual(event, "awaiting_approval")
        self.assertEqual(
            ctx.extras["channel_text"],
            "Step `publish` is waiting for approval to run `google_workspace_docs_append`.\n"
            "Reply *approve* or *reject* in this thread, or decide it in Inbox → Approvals.",
        )

    def test_notice_is_skipped_when_the_hook_engine_is_off(self):
        engine = WorkflowEngine(client=MagicMock(), registry=build_default_registry())
        with patch.dict("os.environ", {"TS_HARNESS_HOOK_ENGINE": "false"}), patch.object(
            engine, "_maybe_notify"
        ) as notify:
            result = engine.notify_tool_approval_waiting(
                {"id": "t", "workflow_id": "wf-1", "workflow_type": "prd_request"}, tool_name="exec"
            )
        self.assertIsNone(result)
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
