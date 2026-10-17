"""Tests for stuck-workflow operability status aggregation."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from unittest.mock import MagicMock

from ts_harness.workflows.hooks import compensate_on_failure
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.operability import build_operability_status
from ts_harness.workflows.registry import HookContext


class OperabilityStatusTests(unittest.TestCase):
    def test_external_wait_payload(self):
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        status = build_operability_status(
            workflow={"definition_version": "1.3.0", "status": "blocked"},
            tasks=[
                {
                    "id": "t-wait",
                    "step_name": "await_human_merge",
                    "status": "blocked",
                    "error": "awaiting_external:github.merge",
                    "updated_at": (now - timedelta(minutes=5)).isoformat(),
                    "input": {"_merge_expected": ["https://github.com/org/repo/pull/1"]},
                }
            ],
            artifacts={"pr_urls": ["https://github.com/org/repo/pull/1"], "qa_status": "success"},
            now=now,
        )
        self.assertEqual(status["waiting_on"]["kind"], "external")
        self.assertEqual(status["waiting_on"]["event_type"], "github.merge")
        self.assertEqual(status["qa_status"], "success")
        self.assertEqual(status["pr_urls"], ["https://github.com/org/repo/pull/1"])
        self.assertIn("merge", status["recommended_action"])

    def test_blocked_qa_payload(self):
        status = build_operability_status(
            workflow={"definition_version": "1.3.0", "status": "blocked"},
            tasks=[
                {
                    "id": "t-qa",
                    "step_name": "qa_gate",
                    "status": "blocked",
                    "error": "qa_status failed",
                    "output": {"status": "failed"},
                }
            ],
            artifacts={},
        )
        self.assertEqual(status["waiting_on"]["kind"], "agent")
        self.assertEqual(status["active_step"], "qa_gate")
        self.assertIn("qa", status["recommended_action"])

    def test_expired_lease_payload(self):
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        status = build_operability_status(
            workflow={"definition_version": "1.3.0", "status": "running"},
            tasks=[
                {
                    "id": "t-run",
                    "step_name": "deliver_work_order",
                    "status": "running",
                    "lock_expires_at": (now - timedelta(minutes=1)).isoformat(),
                    "updated_at": (now - timedelta(minutes=10)).isoformat(),
                }
            ],
            artifacts={},
            now=now,
        )
        self.assertTrue(status["lease"]["expired"])
        self.assertIn("stale_lease", status["recommended_action"])


class CompensateOnFailureTests(unittest.TestCase):
    def test_technical_failure_triggers_compensation_once(self):
        skeleton = load_skeleton("deliver_work_order")
        memory = MagicMock()
        failed = {
            "id": "deliver-1",
            "step_name": "deliver_work_order",
            "error": "coding backend failed",
            "output": {},
        }
        ctx = HookContext(
            workflow_id="wf-1",
            workflow_type="deliver_work_order",
            tenant_id="default",
            task=failed,
            stage=skeleton.stage("deliver_work_order"),
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=memory,
            extras={"error": "coding backend failed"},
        )
        with mock.patch(
            "ts_harness.workflows.hooks.compensate_on_failure.create_tasks_bulk",
            return_value=["comp-1"],
        ) as mock_bulk:
            with mock.patch(
                "ts_harness.workflows.hooks.compensate_on_failure.load_workflow_payload",
                side_effect=[
                    ({"id": "wf-1", "program_id": "prog-1"}, [failed]),
                    (
                        {"id": "wf-1", "program_id": "prog-1"},
                        [
                            failed,
                            {
                                "id": "comp-1",
                                "step_name": "record_implement_failure",
                                "input": {"_failed_task": {"id": "deliver-1"}},
                            },
                        ],
                    ),
                ],
            ):
                first = compensate_on_failure.handle(ctx)
                self.assertEqual(first["status"], "compensating")
                self.assertEqual(first["compensate_stage"], "record_implement_failure")
                memory.patch_program_artifacts.assert_called()
                args = memory.patch_program_artifacts.call_args
                self.assertEqual(args.args[0], "prog-1")
                self.assertEqual(args.args[1]["failed_stage"], "deliver_work_order")
                self.assertEqual(args.args[1]["compensation_status"], "triggered")
                mock_bulk.assert_called_once()

                second = compensate_on_failure.handle(ctx)
                self.assertTrue(second.get("idempotent"))
                self.assertEqual(mock_bulk.call_count, 1)

    def test_qa_blocked_is_not_compensate_path(self):
        # Schema-valid QA failure uses blocked outcome, not on_failure compensation.
        skeleton = load_skeleton("deliver_work_order")
        qa = skeleton.stage("qa_gate")
        self.assertFalse(bool(qa.on_failure))


if __name__ == "__main__":
    unittest.main()
