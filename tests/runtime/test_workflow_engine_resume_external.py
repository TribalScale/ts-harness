"""Regression: resume_external must pass the merge event payload through."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows.engine import WorkflowEngine


class ResumeExternalPayloadTests(unittest.TestCase):
    def test_resume_external_preserves_event_payload(self) -> None:
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"workflow_type": "implement_work_orders"},
            "tasks": [
                {
                    "id": "t-wait",
                    "step_name": "await_human_merge",
                    "status": "succeeded",
                    "output": {"pr_url": "https://github.com/org/app/pull/5"},
                }
            ],
        }
        engine = WorkflowEngine(client=client)
        event_payload = {
            "pr_url": "https://github.com/org/app/pull/5",
            "merge_sha": "abc123",
        }

        with patch(
            "ts_harness.workflows.hooks.wait_external.resume",
            return_value={"status": "resumed", "task_id": "t-wait"},
        ) as resume:
            with patch.object(engine, "on_task_transition") as on_transition:
                result = engine.resume_external(
                    "wf-1",
                    event_type="github.merge",
                    payload=event_payload,
                    actor="github-merge-poller",
                    task_output={"pr_url": event_payload["pr_url"]},
                )

        self.assertEqual(result["status"], "resumed")
        ctx = resume.call_args.args[0]
        self.assertEqual(ctx.extras["event_type"], "github.merge")
        self.assertEqual(ctx.extras["payload"], event_payload)
        self.assertNotIn("workflow", ctx.extras["payload"])
        on_transition.assert_called_once()


if __name__ == "__main__":
    unittest.main()
