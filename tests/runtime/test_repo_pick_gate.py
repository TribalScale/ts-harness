from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord, reset_session_store
from ts_harness.gateway.channel_confirmations import execute_workflow_gate_response
from ts_harness.memory.client import MemoryClient
from ts_harness.workflows.hooks import approve_gate
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.repo_pick import repo_pick_prompt, resolve_repo_pick

HARNESS = {"id": "TribalScale/ts-harness", "default_branch": "dev"}
UI = {"id": "TribalScale/ts-harness-ui", "default_branch": "main"}
ALLOWLIST = "ts_harness.workflows.repo_pick.allowlisted_repos"


class ResolveRepoPickTests(unittest.TestCase):
    def test_single_allowlisted_repo_is_used_without_a_mention(self):
        self.assertEqual(resolve_repo_pick([HARNESS], ["approve", ""]), {"repo": "TribalScale/ts-harness", "default_branch": "dev"})

    def test_reply_mention_picks_among_several(self):
        self.assertEqual(resolve_repo_pick([HARNESS, UI], ["use tribalscale/ts-harness-ui please", ""])["repo"], UI["id"])

    def test_mention_needs_a_whole_repo_id(self):
        self.assertEqual(resolve_repo_pick([HARNESS, UI], ["TribalScale/ts-harness, branch dev", ""])["repo"], HARNESS["id"])

    def test_request_text_is_used_when_the_reply_names_nothing(self):
        self.assertEqual(resolve_repo_pick([HARNESS, UI], ["approve", "Repo: TribalScale/ts-harness-ui"])["repo"], UI["id"])

    def test_several_repos_and_no_mention_is_unresolved(self):
        self.assertIsNone(resolve_repo_pick([HARNESS, UI], ["approve", "the login page is broken"]))
        self.assertIn("TribalScale/ts-harness, TribalScale/ts-harness-ui", repo_pick_prompt([HARNESS, UI]))

    def test_empty_allowlist_fails_closed(self):
        self.assertIsNone(resolve_repo_pick([], ["TribalScale/ts-harness"]))
        self.assertIn("No repos are on the allowlist", repo_pick_prompt([]))


class ApproveGateRepoPickTests(unittest.TestCase):
    def _ctx(self, reply_text: str = "") -> HookContext:
        skeleton = load_skeleton("bug_to_plan")
        client = MagicMock()
        client.approve_task.return_value = {"status": "succeeded"}
        return HookContext(
            workflow_id="wf-1",
            workflow_type="bug_to_plan",
            tenant_id="default",
            task={"id": "gate-1", "step_name": "pick_repo", "input": {"program_id": "prog-1", "summary": "File this bug"}},
            stage=skeleton.stage("pick_repo"),
            skeleton=skeleton,
            actor="orchestrator",
            memory_client=client,
            extras={"decision": {}, "reply_text": reply_text},
        )

    def test_approval_records_the_allowlisted_repo_as_gate_output(self):
        ctx = self._ctx("approve")
        with patch(ALLOWLIST, return_value=[HARNESS]) as allowlist:
            approve_gate.handle(ctx)
        allowlist.assert_called_once_with("prog-1")
        self.assertEqual(
            ctx.memory_client.approve_task.call_args.kwargs["output"],
            {"repo": "TribalScale/ts-harness", "default_branch": "dev"},
        )

    def test_unresolved_repo_raises_the_prompt_and_does_not_approve(self):
        ctx = self._ctx("approve")
        with patch(ALLOWLIST, return_value=[HARNESS, UI]):
            with self.assertRaisesRegex(ValueError, "Reply with one of"):
                approve_gate.handle(ctx)
        ctx.memory_client.approve_task.assert_not_called()

    def test_other_gates_send_no_output(self):
        ctx = self._ctx("approve")
        ctx.stage = replace(ctx.stage, contract_ref="", present_prompt="approval")
        approve_gate.handle(ctx)
        self.assertIsNone(ctx.memory_client.approve_task.call_args.kwargs["output"])

    def test_intake_gate_stores_the_reply(self):
        ctx = self._ctx("ready, draft the bug")
        ctx.stage = replace(ctx.stage, contract_ref="", present_prompt="intake")
        approve_gate.handle(ctx)
        self.assertEqual(
            ctx.memory_client.approve_task.call_args.kwargs["output"],
            {"reply_text": "ready, draft the bug", "summary": "ready, draft the bug"},
        )


class MemoryClientApproveOutputTests(unittest.TestCase):
    def test_output_is_sent_to_memory_api(self):
        client = MemoryClient("http://memory")
        with patch.object(client, "_request", return_value={}) as request:
            client.approve_task("t-1", actor="a", output={"repo": "o/r", "default_branch": "dev"})
        self.assertEqual(request.call_args.args[2]["output"], {"repo": "o/r", "default_branch": "dev"})


class RepoPickChannelReplyTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.session = SessionRecord(
            session_id="slack:C1:t1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
        )
        self.session.metadata = {"workflow_id": "wf-1", "workflow_type": "bug_to_plan", "mode": "workflow_linked"}

    def _gate(self, step_name: str) -> dict:
        return {
            "id": "gate-1",
            "workflow_id": "wf-1",
            "workflow_type": "bug_to_plan",
            "step_name": step_name,
            "status": "pending",
            "approval_required": True,
            "approval_status": "pending",
            "input": {"program_id": "prog-1"},
        }

    def _reply(self, text: str, step_name: str = "pick_repo", repos=(HARNESS, UI)) -> tuple[dict, MagicMock]:
        with patch("ts_harness.workflows.engine.get_workflow_engine") as engine_factory, patch(ALLOWLIST, return_value=list(repos)):
            engine = engine_factory.return_value
            engine.get_status.return_value = {"tasks": [self._gate(step_name)]}
            engine.handle_approve.return_value = {"status": "approved"}
            result = execute_workflow_gate_response(
                self.session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
                user_text=text,
                use_llm_fallback=False,
            )
        return result, engine

    def test_naming_an_allowlisted_repo_approves_with_the_reply(self):
        result, engine = self._reply("TribalScale/ts-harness-ui, default branch main")
        self.assertEqual(result["status"], "approved")
        self.assertEqual(engine.handle_approve.call_args.kwargs["reply_text"], "TribalScale/ts-harness-ui, default branch main")

    def test_unclear_reply_on_repo_gate_lists_the_repos(self):
        result, engine = self._reply("maybe")
        engine.handle_approve.assert_not_called()
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("Reply with one of: TribalScale/ts-harness, TribalScale/ts-harness-ui", result["summary"])

    def test_unclear_reply_on_non_prd_gate_does_not_mention_a_prd(self):
        result, _ = self._reply("maybe", step_name="approve_bug")
        self.assertNotIn("PRD", result["summary"])
        self.assertIn("`approve_bug`", result["summary"])


if __name__ == "__main__":
    unittest.main()
