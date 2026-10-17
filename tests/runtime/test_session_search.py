from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.agent.provider import FakeProvider
from ts_harness.agent.sessions import SessionStore, reset_session_store
from ts_harness.agent.transcript import (
    TranscriptStore,
    get_transcript_store,
    reset_transcript_store,
)
from ts_harness.policy import opa as opa_module
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.server import RuntimeState
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[2]


def _search_then_done():
    return [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-search",
                                "type": "function",
                                "function": {
                                    "name": "search_sessions",
                                    "arguments": json.dumps(
                                        {
                                            "query": "unique-hit-phrase-xyz",
                                            "limit": 10,
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"summary": "Found the prior note."}),
                    }
                }
            ]
        },
    ]


class SearchSessionsToolTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        transcript_file = tempfile.NamedTemporaryFile(
            prefix="search-transcript-",
            suffix=".jsonl",
            delete=False,
        )
        transcript_file.close()
        self.addCleanup(lambda: Path(transcript_file.name).unlink(missing_ok=True))
        reset_transcript_store(TranscriptStore(path=transcript_file.name))
        skills_tmp = tempfile.TemporaryDirectory(prefix="search-skills-")
        self.addCleanup(skills_tmp.cleanup)
        self.skills_dir = Path(skills_tmp.name)
        env = mock.patch.dict(
            os.environ,
            {
                "TS_HARNESS_SKIP_MCP_DISCOVERY": "true",
                "RUNTIME_FAKE_PROVIDER": "true",
                "SKILLS_DIR": str(self.skills_dir),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        opa_patch = mock.patch.object(opa_module, "_opa_client", None)
        opa_patch.start()
        self.addCleanup(opa_patch.stop)
        self.runtime = RuntimeState(
            str(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        )
        self.auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        self.audits: list[dict] = []

        def append_audit(**kwargs):
            self.audits.append(kwargs)

        self.runtime.audit.append = append_audit

    def _message(self, text: str, *, thread_id: str):
        return {
            "tenant_id": "default",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": thread_id,
            "text": text,
        }

    def _spy_run_channel_chat(self):
        original = self.runtime.agent.run_channel_chat
        calls: list[dict] = []

        def wrapper(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)

        return calls, wrapper

    def test_tool_returns_snippet_and_session_id(self):
        reset_session_store(SessionStore())
        from ts_harness.agent.sessions import get_session_store

        session = get_session_store().get_or_create(
            tenant_id="default", source="web", agent_id="orchestrator"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            reset_transcript_store(TranscriptStore(path=path))
            get_transcript_store().append_messages(
                session.session_id,
                [{"role": "user", "content": "checkout PRD last Tuesday"}],
            )
            registry = build_default_registry()
            ctx = ToolPolicyContext(
                tenant_id="default", agent_id="orchestrator", source="web"
            )
            result = registry.dispatch(
                "search_sessions", {"query": "checkout PRD", "limit": 10}, context=ctx
            )
        self.assertIn("hits", result)
        self.assertEqual(result["hits"][0]["session_id"], session.session_id)
        self.assertIn("checkout", result["hits"][0]["snippet"].lower())

    def test_channel_chat_does_not_put_hits_in_skill_prompt(self):
        """Mirror LearnSessionTests RuntimeState setup (RUNTIME_FAKE_PROVIDER, test yaml)."""

        from ts_harness.agent.sessions import get_session_store

        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="t-search",
        )
        get_transcript_store().append_messages(
            session.session_id,
            [{"role": "user", "content": "remember unique-hit-phrase-xyz"}],
        )
        provider = FakeProvider(_search_then_done())
        self.runtime.agent._provider_override = provider
        calls, wrapper = self._spy_run_channel_chat()
        with mock.patch.object(
            self.runtime.agent,
            "run_channel_chat",
            side_effect=wrapper,
        ):
            result = self.runtime.handle_session_message(
                self._message("find the prior note", thread_id="t-search"),
                self.auth,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(calls)
        tool_messages = [
            message
            for message in provider.calls[-1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertTrue(tool_messages)
        self.assertIn("unique-hit-phrase-xyz", tool_messages[-1]["content"])
        self.assertNotIn(
            "unique-hit-phrase-xyz", str(calls[-1].get("skill_prompt") or "")
        )
        self.assertNotIn(
            "unique-hit-phrase-xyz", str(calls[-1].get("system_context") or "")
        )


class SessionSearchAsgiTests(unittest.TestCase):
    def setUp(self):
        reset_session_store(SessionStore())
        transcript_file = tempfile.NamedTemporaryFile(
            prefix="search-admin-transcript-",
            suffix=".jsonl",
            delete=False,
        )
        transcript_file.close()
        self.addCleanup(lambda: Path(transcript_file.name).unlink(missing_ok=True))
        reset_transcript_store(TranscriptStore(path=transcript_file.name))
        self.addCleanup(reset_session_store, SessionStore())
        self.auth = AuthContext(tenant_id="tenant-a", actor_id="admin:U1")
        self.audits: list[dict] = []
        self.runtime = mock.Mock()
        self.runtime.audit.append = lambda **kwargs: self.audits.append(kwargs)

        from ts_harness.gateway import asgi_app

        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
        ]
        for patch in self.patches:
            patch.start()
        from starlette.testclient import TestClient

        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_empty_q_returns_empty_hits(self):
        response = self.client.get(
            "/admin/sessions/search?q=",
            headers={"Authorization": "Bearer test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"hits": []})
        self.assertEqual(self.audits, [])

    def test_search_returns_only_caller_tenant(self):
        from ts_harness.agent.sessions import get_session_store

        store = get_session_store()
        tenant_a = store.get_or_create(
            tenant_id="tenant-a", source="web", agent_id="orchestrator"
        )
        tenant_b = store.get_or_create(
            tenant_id="tenant-b", source="web", agent_id="orchestrator"
        )
        get_transcript_store().append_messages(
            tenant_a.session_id,
            [{"role": "user", "content": "checkout PRD notes for tenant A"}],
        )
        get_transcript_store().append_messages(
            tenant_b.session_id,
            [{"role": "user", "content": "checkout PRD notes for tenant B"}],
        )

        response = self.client.get(
            "/admin/sessions/search?q=checkout",
            headers={"Authorization": "Bearer test"},
        )

        self.assertEqual(response.status_code, 200)
        hits = response.json()["hits"]
        self.assertEqual(
            [hit["session_id"] for hit in hits],
            [tenant_a.session_id],
        )
        self.assertEqual(len(self.audits), 1)
        self.assertEqual(
            self.audits[0],
            {
                "tenant_id": "tenant-a",
                "action_type": "sessions_searched",
                "actor_id": "admin:U1",
                "payload": {"query_length": len("checkout"), "hit_count": 1},
            },
        )
        self.assertNotIn("checkout", self.audits[0]["payload"])


if __name__ == "__main__":
    unittest.main()
