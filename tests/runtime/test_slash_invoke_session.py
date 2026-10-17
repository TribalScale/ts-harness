from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.agent.loop import AgentRunResult
from ts_harness.agent.sessions import get_session_store, reset_session_store
from ts_harness.agent.transcript import TranscriptStore, get_transcript_store, reset_transcript_store
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.server import RuntimeState


REPO_ROOT = Path(__file__).resolve().parents[2]


def _ok_result() -> AgentRunResult:
    return AgentRunResult(status="succeeded", output={"summary": "ok"}, messages=[])


class SlashInvokeSessionTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        transcript_file = tempfile.NamedTemporaryFile(prefix="slash-transcript-", suffix=".jsonl", delete=False)
        transcript_file.close()
        self.addCleanup(lambda: Path(transcript_file.name).unlink(missing_ok=True))
        reset_transcript_store(TranscriptStore(path=transcript_file.name))
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(REPO_ROOT / "skills")
        self.runtime = RuntimeState(str(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"))
        self.auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        self.audits: list[dict] = []
        self.runtime.audit.append = lambda **kwargs: self.audits.append(kwargs)
        agent = self.runtime.config.get_agent("orchestrator")
        resolved, _ = self.runtime.skill_catalog.resolve_for_channel_chat(
            agent,
            self.runtime.config,
            tenant_id="default",
        )
        self.dump_ids = [skill.metadata.id for skill in resolved]
        self.assertGreaterEqual(len(self.dump_ids), 2)
        self.pin_id = self.dump_ids[0]
        self.other_id = self.dump_ids[1]

    def _message(self, text: str, *, thread_id: str):
        return {
            "tenant_id": "default",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": thread_id,
            "text": text,
        }

    def test_no_slash_keeps_category_dump_and_audit_none(self):
        with mock.patch.object(self.runtime.agent, "run_channel_chat", return_value=_ok_result()) as run_chat:
            result = self.runtime.handle_session_message(self._message("check Artemis", thread_id="t-dump"), self.auth)
        self.assertEqual(result["status"], "succeeded")
        run_chat.assert_called_once()
        prompt = run_chat.call_args.kwargs["skill_prompt"]
        self.assertIn(self.pin_id, prompt)
        self.assertIn(self.other_id, prompt)
        chat_audits = [item for item in self.audits if item.get("action_type") == "channel_chat"]
        self.assertTrue(chat_audits)
        self.assertEqual(chat_audits[-1]["payload"]["skill_invoke"], "none")
        self.assertEqual(chat_audits[-1]["payload"]["skill_pins"], [])

    def test_slash_replaces_dump_and_strips_remainder(self):
        with mock.patch.object(self.runtime.agent, "run_channel_chat", return_value=_ok_result()) as run_chat:
            result = self.runtime.handle_session_message(
                self._message(f"/{self.pin_id} rest of it", thread_id="t-pin"),
                self.auth,
            )
        self.assertEqual(result["status"], "succeeded")
        run_chat.assert_called_once()
        self.assertEqual(run_chat.call_args.kwargs["text"], "rest of it")
        prompt = run_chat.call_args.kwargs["skill_prompt"]
        self.assertIn(self.pin_id, prompt)
        self.assertNotIn(self.other_id, prompt)
        chat_audits = [item for item in self.audits if item.get("action_type") == "channel_chat"]
        self.assertEqual(chat_audits[-1]["payload"]["skill_invoke"], "replaced")
        self.assertEqual(chat_audits[-1]["payload"]["skill_pins"], [self.pin_id])
        stored = get_transcript_store().load_messages(result["session_id"])
        user_turns = [item["content"] for item in stored if item.get("role") == "user"]
        self.assertIn(f"/{self.pin_id} rest of it", user_turns)
        self.assertNotIn("rest of it", user_turns)

    def test_unknown_path_keeps_category_dump(self):
        with mock.patch.object(self.runtime.agent, "run_channel_chat", return_value=_ok_result()) as run_chat:
            result = self.runtime.handle_session_message(
                self._message("/tmp/scan.pdf extract", thread_id="t-path"),
                self.auth,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(run_chat.call_args.kwargs["text"], "/tmp/scan.pdf extract")
        prompt = run_chat.call_args.kwargs["skill_prompt"]
        self.assertIn(self.pin_id, prompt)
        self.assertIn(self.other_id, prompt)
        chat_audits = [item for item in self.audits if item.get("action_type") == "channel_chat"]
        self.assertEqual(chat_audits[-1]["payload"]["skill_invoke"], "none")

    def test_unresolved_pin_blocks_without_model_or_transcript(self):
        with mock.patch.object(self.runtime.agent, "run_channel_chat") as run_chat:
            result = self.runtime.handle_session_message(
                self._message("/jira-to-pr rest", thread_id="t-block"),
                self.auth,
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"], "skill_invoke_unresolved")
        self.assertIn("jira-to-pr", result["output"]["summary"])
        run_chat.assert_not_called()
        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="t-block",
        )
        messages = get_transcript_store().load_messages(session.session_id)
        self.assertEqual(messages, [])
        blocked = [item for item in self.audits if (item.get("payload") or {}).get("skill_invoke") == "blocked"]
        self.assertTrue(blocked)

    def test_yes_confirmation_does_not_call_model(self):
        with mock.patch.object(self.runtime.agent, "run_channel_chat") as run_chat:
            first = self.runtime.handle_session_message(
                self._message(
                    "Create a PRD for an online multiplayer checker game",
                    thread_id="t-yes",
                ),
                self.auth,
            )
            self.assertEqual(first["status"], "succeeded")
            second = self.runtime.handle_session_message(
                self._message("yes", thread_id="t-yes"),
                self.auth,
            )
        run_chat.assert_not_called()
        self.assertNotEqual(second.get("error"), "skill_invoke_unresolved")

    def test_slack_webhook_posts_blocked_summary(self):
        sent: list[dict] = []

        class FakeRouter:
            def normalize(self, channel, body, tenant_id=None):
                from ts_harness.gateway.channels.base import MessageEvent

                return MessageEvent(
                    channel="slack",
                    tenant_id=tenant_id or "default",
                    text=str(body.get("text", "")),
                    sender_id=str(body.get("user", "U1")),
                    thread_id=body.get("thread_ts") or "ts-block",
                    metadata={"channel_id": body.get("channel", "C123"), "channel": body.get("channel", "C123")},
                )

            def send(self, channel, args, *, tenant_id: str):
                sent.append(args)
                return {"status": "sent", "channel": channel}

        with mock.patch("ts_harness.gateway.server.get_channel_router", return_value=FakeRouter()), mock.patch.object(
            self.runtime.agent, "run_channel_chat"
        ) as run_chat:
            result = self.runtime.handle_channel_webhook(
                "slack",
                {
                    "text": "/jira-to-pr x",
                    "channel": "C123",
                    "user": "U1",
                    "thread_ts": "ts-block-wh",
                    "ts": "ts-block-wh",
                    "event_id": "EvSlashBlock",
                },
                self.auth,
            )

        run_chat.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(sent)
        self.assertIn("jira-to-pr", sent[0]["text"])
        self.assertEqual(sent[0]["channel"], "C123")
