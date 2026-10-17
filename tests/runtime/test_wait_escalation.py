"""Wait deadlines, the escalate_stale_waits job, and deadline fields on operability status."""

from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ts_harness.gateway import system_jobs
from ts_harness.workflows.hooks import wait_external
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.operability import build_operability_status
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.wait_deadlines import (
    DEFAULT_ACTION,
    GATE_ACTION,
    recommended_action,
    wait_deadlines,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_waits", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoutedCursor:
    def __init__(self, routes: list[tuple[str, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, Any]] = []
        self._last: Any = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = None
        for fragment, result in self.routes:
            if fragment in sql:
                self._last = result
                break

    def fetchone(self):
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return self._last

    def fetchall(self):
        if isinstance(self._last, list):
            return self._last
        return [self._last] if self._last else []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def audit_actions(self) -> list[str]:
        return [params[2] for sql, params in self.calls if "INSERT INTO session_audit_log" in sql]

    def updates(self, fragment: str) -> list[Any]:
        return [params for sql, params in self.calls if sql.lstrip().startswith("UPDATE") and fragment in sql]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


TASK_ID = "11111111-1111-1111-1111-111111111111"


class WaitDeadlineConfigTests(unittest.TestCase):
    def test_timeout_escalates_without_expiry(self):
        self.assertEqual(wait_deadlines({"timeout_seconds": 86400}), {"escalate_after_seconds": 86400})

    def test_explicit_escalation_and_expiry(self):
        self.assertEqual(
            wait_deadlines({"timeout_seconds": 60, "escalate_after_seconds": 30, "expires_after_seconds": 90}),
            {"escalate_after_seconds": 30, "expires_after_seconds": 90},
        )

    def test_missing_or_invalid_values_are_ignored(self):
        self.assertEqual(wait_deadlines(None), {})
        self.assertEqual(wait_deadlines({"timeout_seconds": "soon", "expires_after_seconds": True}), {})

    def test_merge_wait_escalates_and_never_expires(self):
        merge = load_skeleton("deliver_work_order").stage("await_human_merge")
        deadlines = wait_deadlines(merge.wait_external)
        self.assertIn("escalate_after_seconds", deadlines)
        self.assertNotIn("expires_after_seconds", deadlines)

    def test_recommended_actions(self):
        self.assertEqual(recommended_action({"expected_event_type": "github.merge"}), "review_and_merge_or_cancel_pr")
        self.assertEqual(
            recommended_action({"expected_event_type": "github.deployment_status"}), "inspect_deployment_or_rollback"
        )
        self.assertEqual(recommended_action({"approval_required": True, "expected_event_type": None}), GATE_ACTION)
        self.assertEqual(recommended_action({"expected_event_type": "slack.reply"}), DEFAULT_ACTION)


class WaitExternalDeadlineTests(unittest.TestCase):
    def test_handle_passes_deadlines_to_set_task_wait(self):
        skeleton = load_skeleton("deliver_work_order")
        memory = mock.MagicMock()
        memory.get_workflow.return_value = {"workflow": {"id": "wf-1"}, "tasks": []}
        ctx = HookContext(
            "wf-1",
            "deliver_work_order",
            "default",
            {"id": "t-merge", "input": {}},
            skeleton.stage("await_human_merge"),
            skeleton,
            "workflow-runner",
            memory,
            {},
        )
        wait_external.handle(ctx)
        kwargs = memory.set_task_wait.call_args.kwargs
        self.assertEqual(kwargs["escalate_after_seconds"], 86400)
        self.assertNotIn("expires_after_seconds", kwargs)


class GatewayDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()

    def test_set_task_wait_stores_deadlines(self):
        cursor = RoutedCursor([])
        before = datetime.now(timezone.utc)
        result = self.gateway.set_task_wait_state(
            FakeConnection(cursor),
            {
                "id": TASK_ID,
                "wait_token": "tok",
                "expected_event_type": "github.merge",
                "escalate_after_seconds": 3600,
                "expires_after_seconds": 7200,
            },
        )
        params = cursor.updates("escalate_at")[0]
        self.assertEqual(params[0:2], ("tok", "github.merge"))
        self.assertGreaterEqual(params[2], before + timedelta(seconds=3600))
        self.assertEqual(params[3] - params[2], timedelta(seconds=3600))
        self.assertEqual(result["expires_at"], params[3])

    def test_set_task_wait_without_deadlines_clears_them(self):
        cursor = RoutedCursor([])
        self.gateway.set_task_wait_state(FakeConnection(cursor), {"id": TASK_ID, "wait_token": "tok"})
        params = cursor.updates("escalate_at")[0]
        self.assertIsNone(params[2])
        self.assertIsNone(params[3])

    def test_due_waits_classify_escalated_and_expired(self):
        now = datetime.now(timezone.utc)
        rows = [
            {
                "id": "t-escalate",
                "workflow_id": "wf-1",
                "step_name": "await_human_merge",
                "status": "blocked",
                "expected_event_type": "github.merge",
                "escalate_at": now - timedelta(minutes=5),
                "expires_at": None,
                "input": {},
            },
            {
                "id": "t-expire",
                "workflow_id": "wf-2",
                "step_name": "approve_prd",
                "status": "pending",
                "approval_required": True,
                "escalate_at": now - timedelta(hours=2),
                "expires_at": now - timedelta(hours=1),
                "escalated_at": now - timedelta(hours=2),
                "input": json.dumps({"_approval_policy": {"escalate_to_role": "eng_manager"}}),
            },
        ]
        cursor = RoutedCursor([("input -> '_approval_policy' ?", []), ("escalated_at IS NULL", rows)])
        waits = self.gateway.list_due_waits(FakeConnection(cursor), {"tenant_id": "acme"})["waits"]
        self.assertEqual([(w["task_id"], w["due"]) for w in waits], [("t-escalate", "escalated"), ("t-expire", "expired")])
        self.assertEqual(waits[1]["approval_policy"]["escalate_to_role"], "eng_manager")

    def test_due_waits_stamp_ready_gate_deadlines(self):
        completed = datetime.now(timezone.utc) - timedelta(hours=3)
        gate = {
            "id": "t-gate",
            "depends_on": ["t-dep"],
            "input": {"_approval_policy": {"expires_after_seconds": 3600}},
            "created_at": completed,
        }
        cursor = RoutedCursor(
            [
                ("input -> '_approval_policy' ?", [gate]),
                ("COUNT(*) AS open", {"open": 0}),
                ("MAX(completed_at)", {"ready_at": completed}),
                ("escalated_at IS NULL", []),
            ]
        )
        self.gateway.list_due_waits(FakeConnection(cursor), {})
        stamped = cursor.updates("SET expires_at")[0]
        self.assertEqual(stamped, (completed + timedelta(hours=1), completed + timedelta(hours=1), "t-gate"))

    def test_gate_with_unfinished_dependencies_is_not_stamped(self):
        gate = {"id": "t-gate", "depends_on": ["t-dep"], "input": {"_approval_policy": {"expires_after_seconds": 60}}}
        cursor = RoutedCursor(
            [("input -> '_approval_policy' ?", [gate]), ("COUNT(*) AS open", {"open": 1}), ("escalated_at IS NULL", [])]
        )
        self.gateway.list_due_waits(FakeConnection(cursor), {})
        self.assertEqual(cursor.updates("SET expires_at"), [])

    def test_mark_expired_keeps_task_blocked_and_audits(self):
        cursor = RoutedCursor([])
        result = self.gateway.mark_wait_deadline(
            FakeConnection(cursor),
            {"id": TASK_ID, "kind": "expired", "recommended_action": "review_and_merge_or_cancel_pr"},
        )
        self.assertEqual(result["kind"], "expired")
        sql = next(sql for sql, _ in cursor.calls if sql.lstrip().startswith("UPDATE"))
        self.assertIn("_recommended_action", sql)
        self.assertNotIn("status", sql)
        self.assertEqual(cursor.audit_actions(), ["workflow_wait_expired"])

    def test_mark_escalated_audits(self):
        cursor = RoutedCursor([])
        self.gateway.mark_wait_deadline(FakeConnection(cursor), {"id": TASK_ID, "kind": "escalated"})
        self.assertEqual(cursor.audit_actions(), ["workflow_wait_escalated"])

    def test_mark_rejects_bad_input(self):
        with self.assertRaisesRegex(ValueError, "kind must be one of"):
            self.gateway.mark_wait_deadline(FakeConnection(RoutedCursor([])), {"id": TASK_ID, "kind": "failed"})
        with self.assertRaisesRegex(ValueError, "recommended_action is required"):
            self.gateway.mark_wait_deadline(FakeConnection(RoutedCursor([])), {"id": TASK_ID, "kind": "expired"})


class EscalateStaleWaitsJobTests(unittest.TestCase):
    def _memory(self, waits: list[dict[str, Any]]) -> mock.MagicMock:
        memory = mock.MagicMock()
        memory.list_due_waits.return_value = {"waits": waits}
        return memory

    def test_escalates_and_expires_with_recommended_action(self):
        memory = self._memory(
            [
                {"task_id": "t-1", "workflow_id": "wf-1", "step_name": "await_human_merge", "due": "escalated", "expected_event_type": "github.merge"},
                {
                    "task_id": "t-2",
                    "workflow_id": "wf-2",
                    "step_name": "approve_prd",
                    "due": "expired",
                    "approval_required": True,
                    "approval_policy": {"escalate_to_role": "eng_manager"},
                },
            ]
        )
        notify = mock.MagicMock(return_value=True)
        result = system_jobs.escalate_stale_waits(tenant_id="acme", memory=memory, notify=notify)
        self.assertEqual(result, {"checked": 2, "escalated": 1, "expired": 1, "errors": 0})
        memory.list_due_waits.assert_called_once_with(tenant_id="acme")
        first, second = memory.mark_wait_deadline.call_args_list
        self.assertEqual(first.args, ("t-1",))
        self.assertEqual(first.kwargs["kind"], "escalated")
        self.assertEqual(first.kwargs["recommended_action"], "")
        self.assertEqual(second.kwargs["kind"], "expired")
        self.assertEqual(second.kwargs["recommended_action"], GATE_ACTION)
        self.assertEqual(second.kwargs["details"]["escalate_to_role"], "eng_manager")
        self.assertEqual(notify.call_args.kwargs["role"], "eng_manager")

    def test_notify_failure_still_records_deadline(self):
        memory = self._memory([{"task_id": "t-1", "due": "escalated"}])
        notify = mock.MagicMock(side_effect=RuntimeError("slack down"))
        result = system_jobs.escalate_stale_waits(memory=memory, notify=notify)
        self.assertEqual(result["escalated"], 1)
        self.assertFalse(memory.mark_wait_deadline.call_args.kwargs["details"]["notified"])

    def test_record_failure_counts_as_error(self):
        memory = self._memory([{"task_id": "t-1", "due": "expired"}])
        memory.mark_wait_deadline.side_effect = RuntimeError("gateway down")
        result = system_jobs.escalate_stale_waits(memory=memory, notify=mock.MagicMock(return_value=False))
        self.assertEqual(result, {"checked": 1, "escalated": 0, "expired": 0, "errors": 1})

    def test_job_is_registered(self):
        self.assertIs(system_jobs.SYSTEM_HANDLERS["escalate_stale_waits"], system_jobs.escalate_stale_waits)
        jobs = json.loads((REPO_ROOT / "config" / "runtime-cron-jobs.json").read_text())["jobs"]
        job = next(job for job in jobs if job.get("handler") == "escalate_stale_waits")
        self.assertEqual(job["interval_seconds"], 900)
        phase = (REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh").read_text()
        self.assertIn('"handler": "escalate_stale_waits"', phase)

    def test_bound_channel_falls_back_to_parent_workflow(self):
        memory = mock.MagicMock()
        memory.get_workflow.side_effect = lambda wf: {
            "child": {"workflow": {"id": "child", "parent_workflow_id": "parent"}},
            "parent": {"workflow": {"id": "parent", "source_channel": "slack:C1:123.4"}},
        }[wf]
        self.assertEqual(system_jobs._bound_channel(memory, "child"), ("C1", "123.4"))

    def test_escalation_text_mentions_role_holders(self):
        identity = SimpleNamespace(
            roles={"lead@example.com": ["Eng_Manager"], "dev@example.com": ["developer"]},
            slack_user_emails={"U1": "lead@example.com", "U2": "dev@example.com"},
        )
        runtime = SimpleNamespace(config=SimpleNamespace(identity=identity))
        with mock.patch("ts_harness.gateway.server.get_runtime", return_value=runtime):
            mentions = system_jobs._role_slack_mentions("eng_manager")
        self.assertEqual(mentions, ["<@U1>"])
        text = system_jobs._escalation_text(
            {"step_name": "approve_prd"}, kind="expired", role="eng_manager", action=GATE_ACTION, mentions=mentions
        )
        self.assertIn(GATE_ACTION, text)
        self.assertIn("Escalated to eng_manager: <@U1>", text)


class OperabilityDeadlineTests(unittest.TestCase):
    def test_expired_wait_surfaces_recommended_action_and_escalation(self):
        now = datetime.now(timezone.utc)
        status = build_operability_status(
            workflow={"id": "wf-1"},
            tasks=[
                {
                    "id": "t-merge",
                    "step_name": "await_human_merge",
                    "status": "blocked",
                    "error": "awaiting_external:github.merge",
                    "escalated_at": now.isoformat(),
                    "expires_at": now.isoformat(),
                    "input": {"_recommended_action": "review_and_merge_or_cancel_pr"},
                }
            ],
        )
        self.assertTrue(status["waiting_on"]["escalated"])
        self.assertEqual(status["recommended_action"], "review_and_merge_or_cancel_pr")

    def test_unescalated_gate_keeps_default_action(self):
        status = build_operability_status(
            workflow={"id": "wf-1"},
            tasks=[{"id": "t-gate", "step_name": "approve_prd", "status": "pending", "approval_required": True, "approval_status": "pending"}],
        )
        self.assertFalse(status["waiting_on"]["escalated"])
        self.assertEqual(status["recommended_action"], "approve_or_reject_pending_gate")


if __name__ == "__main__":
    unittest.main()
