"""Chat posts follow the open session, not a leftover browser binding."""

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


def _ok(summary: str) -> AgentRunResult:
    return AgentRunResult(status="succeeded", output={"summary": summary}, messages=[])


class SessionMessageRoutingTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        transcript_file = tempfile.NamedTemporaryFile(prefix="route-transcript-", suffix=".jsonl", delete=False)
        transcript_file.close()
        self.addCleanup(lambda: Path(transcript_file.name).unlink(missing_ok=True))
        reset_transcript_store(TranscriptStore(path=transcript_file.name))
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        self.runtime = RuntimeState(str(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"))
        self.auth = AuthContext(tenant_id="default", actor_id="operator-1")
        self.runtime.audit.append = lambda **_kwargs: None

    def _post(self, **extra):
        body = {"tenant_id": "default", "channel": "web", "text": "hello"}
        body.update(extra)
        with mock.patch.object(self.runtime.agent, "run_channel_chat", return_value=_ok("ok")):
            return self.runtime.handle_session_message(body, self.auth)

    def test_named_session_wins_over_a_stale_binding(self):
        opened = self._post(text="question", thread_id="web:open-chat", binding_key="web:open-chat")
        open_id = opened["session_id"]

        routed = self._post(
            text="the intake answer",
            session_id=open_id,
            thread_id="web:leftover-chat",
            binding_key="web:leftover-chat",
        )

        self.assertEqual(routed["session_id"], open_id)
        stored = get_session_store().get(open_id)
        assert stored is not None
        self.assertEqual(stored.metadata.get("binding_key"), "web:open-chat")
        messages = get_transcript_store().load_messages(open_id)
        self.assertIn("the intake answer", [item.get("content") for item in messages])
        leftover = get_session_store().get_or_create(
            tenant_id="default",
            source="web",
            agent_id=stored.agent_id,
            channel="web",
            thread_id="web:leftover-chat",
        )
        self.assertNotEqual(leftover.session_id, open_id)
        self.assertEqual(get_transcript_store().load_messages(leftover.session_id), [])

    def test_new_chat_still_follows_its_binding(self):
        created = self._post(text="start", thread_id="web:fresh", binding_key="web:fresh")
        again = self._post(text="continue", thread_id="web:fresh", binding_key="web:fresh")
        self.assertEqual(again["session_id"], created["session_id"])
