"""/revise, /accept and /stop replies for steps waiting on a human decision."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord
from ts_harness.gateway.channel_confirmations import (
    execute_stuck_step_command,
    format_stuck_step_ack,
    parse_stuck_step_command,
)


def _summary(work_order, task_id, program_id="prog-1"):
    return {
        "workflow_id": f"wf-{task_id}",
        "task_id": task_id,
        "step_name": "finalize_code_review",
        "work_order": work_order,
        "decisions": ["revise", "accept", "stop"],
        "program_id": program_id,
    }


class StuckStepCommandTests(unittest.TestCase):
    def setUp(self):
        self.session = SessionRecord(
            session_id="s1",
            routing_key="default:web",
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
            metadata={"program_id": "prog-1"},
        )
        self.memory = MagicMock()
        self.engine = MagicMock()
        self.engine.decide_blocked_step.side_effect = lambda wid, tid, action, **kw: {"status": {"revise": "revising", "accept": "accepted", "stop": "stopped"}[action], "work_order": "WO-003", "revision_number": 1, "max_revisions": 3}
        self.summaries = {}
        patcher = patch(
            "ts_harness.workflows.human_decisions.stuck_summary",
            side_effect=lambda engine, wid, tid: self.summaries[tid],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        allow = patch("ts_harness.workflows.decisions.channel_permission_refusal", return_value=None)
        self.permission = allow.start()
        self.addCleanup(allow.stop)

    def _stuck(self, *summaries):
        self.summaries = {s["task_id"]: s for s in summaries}
        self.memory.list_stuck_tasks.return_value = [{"id": s["task_id"], "workflow_id": s["workflow_id"]} for s in summaries]

    def _run(self, text):
        return execute_stuck_step_command(
            self.session,
            user_text=text,
            actor_id="alice",
            channel="web",
            auth=MagicMock(actor_id="alice"),
            memory=self.memory,
            engine=self.engine,
        )

    def test_parse(self):
        self.assertEqual(parse_stuck_step_command("/revise WO-003 persist the empty state"), ("revise", "WO-003 persist the empty state"))
        self.assertEqual(parse_stuck_step_command("  /ACCEPT "), ("accept", ""))
        self.assertEqual(parse_stuck_step_command("/stop"), ("stop", ""))
        self.assertIsNone(parse_stuck_step_command("please revise it"))
        self.assertIsNone(parse_stuck_step_command("/revisex"))

    def test_single_stuck_step_needs_no_work_order(self):
        self._stuck(_summary("WO-003", "t3"))
        result = self._run("/revise Persist the empty state.")
        self.engine.decide_blocked_step.assert_called_once_with(
            "wf-t3", "t3", "revise", actor="alice", guidance="Persist the empty state.", tenant_id="default"
        )
        self.assertEqual(result["status"], "revising")

    def test_work_order_picks_the_step_and_the_rest_is_guidance(self):
        self._stuck(_summary("WO-003", "t3"), _summary("WO-004", "t4"))
        self._run("/revise wo-004 Use the shared store")
        args, kwargs = self.engine.decide_blocked_step.call_args
        self.assertEqual((args[1], kwargs["guidance"]), ("t4", "Use the shared store"))

    def test_several_stuck_steps_without_a_work_order_asks_which(self):
        self._stuck(_summary("WO-003", "t3"), _summary("WO-004", "t4"))
        result = self._run("/accept")
        self.engine.decide_blocked_step.assert_not_called()
        self.assertIn("WO-003", result["error"])
        self.assertIn("WO-004", result["error"])
        self.assertIn("/accept WO-003", format_stuck_step_ack(result))

    def test_only_this_programs_steps_count(self):
        self._stuck(_summary("WO-009", "t9", program_id="other"))
        result = self._run("/stop")
        self.engine.decide_blocked_step.assert_not_called()
        self.assertIn("Nothing in this program is waiting", format_stuck_step_ack(result))

    def test_permission_is_required(self):
        self._stuck(_summary("WO-003", "t3"))
        self.permission.return_value = "missing permission approve_gate"
        result = self._run("/accept")
        self.assertEqual(result["status"], "denied")
        self.engine.decide_blocked_step.assert_not_called()

    def test_acks(self):
        self.assertIn("revision 1/3", format_stuck_step_ack({"status": "revising", "work_order": "WO-003", "revision_number": 1, "max_revisions": 3}))
        self.assertIn("Accepted WO-003", format_stuck_step_ack({"status": "accepted", "work_order": "WO-003"}))
        self.assertIn("Stopped WO-003", format_stuck_step_ack({"status": "stopped", "work_order": "WO-003"}))
        self.assertIn("not waiting", format_stuck_step_ack({"error": "step is not waiting for a decision"}))


if __name__ == "__main__":
    unittest.main()
