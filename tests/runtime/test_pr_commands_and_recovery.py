"""PR comments decide a stuck step; a restart's unfinished block handling is replayed."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ts_harness.gateway import github_webhook, pr_commands
from ts_harness.workflows import block_recovery

PR = "https://github.com/org/api/pull/4"
STUCK = {"workflow_id": "wf-1", "task_id": "gate-1", "work_order": "PROJ-3", "program_id": "prog-1", "pr_url": PR}


def _config(provider="static"):
    return SimpleNamespace(identity=SimpleNamespace(provider=provider, github_logins={"dana@example.com": "dana"}, roles={}))


class PrCommandTests(unittest.TestCase):
    def run_command(self, body, *, author="dana", stuck=(STUCK,), refusal=None, roles=("approver",)):
        memory, engine, said = MagicMock(), MagicMock(), []
        memory.list_stuck_tasks.return_value = [{"id": "gate-1", "workflow_id": "wf-1"}]
        memory.directory_user.return_value = {"subject": "s1", "roles": list(roles), "teams": []}
        engine.decide_blocked_step.return_value = {"status": "revising"}
        with patch("ts_harness.workflows.human_decisions.stuck_summary", side_effect=list(stuck) or [{}]), \
                patch("ts_harness.workflows.decisions.refusal_for_permission", return_value=refusal):
            result = pr_commands.handle_pr_command(
                {"pr_url": PR, "author": author, "body": body}, memory=memory, engine=engine, config=_config(),
                reply=lambda url, text: said.append(text))
        return result, engine, said

    def test_revise_accept_and_stop_decide_the_stuck_step(self):
        result, engine, said = self.run_command("/revise Use the advisory lock.")
        self.assertEqual(result["status"], "decided")
        engine.decide_blocked_step.assert_called_once_with("wf-1", "gate-1", "revise", actor="dana@example.com",
                                                           guidance="Use the advisory lock.", tenant_id="default")
        self.assertIn("sent PROJ-3 back", said[0])
        _, engine, _ = self.run_command("/accept")
        self.assertEqual(engine.decide_blocked_step.call_args.args[2], "accept")
        _, engine, _ = self.run_command("/stop PROJ-3")
        self.assertEqual(engine.decide_blocked_step.call_args.args[2:], ("stop",))

    def test_unknown_commenters_and_missing_permission_are_refused_on_the_pr(self):
        result, engine, said = self.run_command("/accept", author="stranger")
        self.assertEqual(result["status"], "unmapped")
        self.assertIn("not mapped", said[0])
        engine.decide_blocked_step.assert_not_called()
        result, engine, said = self.run_command("/accept", refusal="missing permission approve_gate")
        self.assertEqual(result["status"], "denied")
        self.assertIn("missing permission approve_gate", said[0])

    def test_nothing_waiting_falls_back(self):
        result, engine, _ = self.run_command("/revise x", stuck=({**STUCK, "pr_url": "https://github.com/org/api/pull/9"},))
        self.assertEqual(result["status"], "no_stuck_step")
        engine.decide_blocked_step.assert_not_called()
        self.assertIsNone(pr_commands.parse_command("thanks /revise"))
        self.assertEqual(pr_commands.parse_command("/Accept\n"), ("accept", ""))

    def test_the_webhook_routes_all_three_commands(self):
        for text in ("/revise fix it", "/accept", "/stop"):
            event = github_webhook.parse_github_event({
                "action": "created", "issue": {"number": 4, "html_url": PR, "pull_request": {}},
                "comment": {"body": text, "user": {"login": "dana"}}, "repository": {"full_name": "org/api"}})
            self.assertEqual((event["kind"], event["body"]), ("github.pr_comment", text))
        with patch("ts_harness.gateway.pr_commands.handle_pr_command", return_value={"status": "decided"}) as decide, \
                patch.object(github_webhook, "notify_subscribers", return_value=[]), \
                patch.object(github_webhook, "_handle_pr_feedback") as feedback:
            out = github_webhook.handle_github_webhook({
                "action": "created", "issue": {"number": 4, "html_url": PR, "pull_request": {}},
                "comment": {"body": "/accept", "user": {"login": "dana"}}, "repository": {"full_name": "org/api"}})
        self.assertEqual(out["status"], "decided")
        decide.assert_called_once()
        feedback.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        block_recovery._attempted.clear()

    def _engine(self, task, on_block):
        engine = MagicMock()
        engine.get_status.return_value = {"workflow": {}, "tasks": [task]}
        engine._skeleton_for_state.return_value.stage.return_value = SimpleNamespace(on_block=on_block)
        return engine

    def test_an_unhandled_block_is_replayed_once(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task = {"id": "gate-1", "workflow_id": "wf-1", "step_name": "finalize_code_review", "status": "blocked",
                "error": "barrier_blocked: code_review_blocked", "input": {}, "output": {"verdict": "block"}, "updated_at": old}
        memory = MagicMock()
        memory.list_stuck_tasks.return_value = [task]
        engine = self._engine(task, {"hook": "revise_on_reject"})
        self.assertEqual(block_recovery.recover_unhandled_blocks(memory, engine), ["gate-1"])
        engine.on_task_transition.assert_called_once()
        self.assertEqual(engine.on_task_transition.call_args.args[1], "blocked")
        # The same task is not replayed again on the next sweep.
        self.assertEqual(block_recovery.recover_unhandled_blocks(memory, engine), [])

    def test_waiting_recent_or_loopless_blocks_are_left_alone(self):
        now = datetime.now(timezone.utc)
        base = {"id": "t", "workflow_id": "wf", "step_name": "x", "status": "blocked", "error": "barrier_blocked: x", "input": {},
                "updated_at": (now - timedelta(minutes=5)).isoformat()}
        loop = SimpleNamespace(on_block={"hook": "revise_on_reject"})
        self.assertTrue(block_recovery.unhandled(base, loop, now=now))
        self.assertFalse(block_recovery.unhandled({**base, "input": {"_awaiting_human_decision": {"reason": "r"}}}, loop, now=now))
        self.assertFalse(block_recovery.unhandled({**base, "error": "awaiting_external:github.merge"}, loop, now=now))
        self.assertFalse(block_recovery.unhandled({**base, "updated_at": now.isoformat()}, loop, now=now))
        self.assertFalse(block_recovery.unhandled(base, SimpleNamespace(on_block=None), now=now))


if __name__ == "__main__":
    unittest.main()
