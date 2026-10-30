import os
import tempfile
import unittest
from pathlib import Path

from ts_harness.agent.sessions import SessionStore, reset_session_store
from ts_harness.agent.transcript import (
    TranscriptStore,
    reset_transcript_store,
)
from ts_harness.tools.policy import redact_snippet


class TranscriptStoreTests(unittest.TestCase):
    def test_jsonl_append_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcripts.jsonl"
            store = TranscriptStore(path=path)
            store.append_messages(
                "sess-1",
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi there"},
                ],
            )
            store.append_messages(
                "sess-1",
                [{"role": "user", "content": "follow up"}],
            )
            messages = store.load_messages("sess-1")
            self.assertEqual(len(messages), 3)
            self.assertEqual(messages[0]["content"], "hello")
            self.assertEqual(messages[-1]["content"], "follow up")

    def test_append_turn_stores_user_and_assistant_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcripts.jsonl"
            store = TranscriptStore(path=path)
            store.append_turn(
                "sess-2",
                user_text="what agents?",
                assistant_messages=[
                    {"role": "assistant", "content": "We have dev-agent and ops-agent."},
                ],
            )
            loaded = store.load_messages("sess-2")
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["role"], "user")
            self.assertEqual(loaded[1]["role"], "assistant")

    def test_clear_removes_session_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcripts.jsonl"
            store = TranscriptStore(path=path)
            store.append_messages("sess-a", [{"role": "user", "content": "a"}])
            store.append_messages("sess-b", [{"role": "user", "content": "b"}])
            store.clear("sess-a")
            self.assertEqual(store.load_messages("sess-a"), [])
            self.assertEqual(len(store.load_messages("sess-b")), 1)

    def test_load_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcripts.jsonl"
            store = TranscriptStore(path=path)
            for i in range(10):
                store.append_messages("sess-l", [{"role": "user", "content": str(i)}])
            messages = store.load_messages("sess-l", limit=3)
            self.assertEqual(len(messages), 3)
            self.assertEqual(messages[0]["content"], "7")
            self.assertEqual(messages[-1]["content"], "9")

    def test_get_transcript_store_singleton(self):
        reset_transcript_store(TranscriptStore(path=None))
        from ts_harness.agent import transcript as mod

        mod._default_store = None
        store = mod.get_transcript_store()
        self.assertIs(store, mod.get_transcript_store())

    def test_redact_snippet_strips_bearer_and_truncates(self):
        raw = (
            "checkout " * 20
            + "Authorization: Bearer abc-secret-past-120 "
            + ("x" * 300)
        )
        out = redact_snippet(raw)
        self.assertNotIn("abc-secret-past-120", out)
        self.assertIn("Authorization: Bearer [REDACTED]", out)
        self.assertLessEqual(len(out), 240)

    def test_jsonl_search_is_tenant_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_session_store(SessionStore())
            from ts_harness.agent.sessions import get_session_store

            store = get_session_store()
            a = store.get_or_create(tenant_id="tenant-a", source="web", agent_id="orchestrator")
            b = store.get_or_create(tenant_id="tenant-b", source="web", agent_id="orchestrator")
            path = Path(tmp) / "transcripts.jsonl"
            transcripts = TranscriptStore(path=path)
            transcripts.append_messages(a.session_id, [{"role": "user", "content": "checkout PRD notes"}])
            transcripts.append_messages(b.session_id, [{"role": "user", "content": "checkout PRD notes"}])
            hits_a = transcripts.search(tenant_id="tenant-a", query="checkout PRD")
            hits_b = transcripts.search(tenant_id="tenant-b", query="checkout PRD")
            self.assertEqual([h.session_id for h in hits_a], [a.session_id])
            self.assertEqual([h.session_id for h in hits_b], [b.session_id])
            self.assertTrue(all(h.role == "user" for h in hits_a))
            self.assertIn("checkout", hits_a[0].snippet.lower())
            self.addCleanup(reset_session_store, SessionStore())

    def test_jsonl_search_skips_tool_rows_and_empty_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_session_store(SessionStore())
            from ts_harness.agent.sessions import get_session_store

            session = get_session_store().get_or_create(
                tenant_id="default", source="web", agent_id="orchestrator"
            )
            path = Path(tmp) / "transcripts.jsonl"
            transcripts = TranscriptStore(path=path)
            transcripts.append_messages(
                session.session_id,
                [
                    {"role": "user", "content": "needle in haystack"},
                    {"role": "tool", "content": "needle tool output"},
                    {"role": "assistant", "content": "found the needle"},
                ],
            )
            hits = transcripts.search(tenant_id="default", query="needle")
            self.assertEqual({h.role for h in hits}, {"user", "assistant"})
            self.assertEqual(transcripts.search(tenant_id="default", query="   "), [])
            self.addCleanup(reset_session_store, SessionStore())

    def test_jsonl_search_returns_newest_limited_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_session_store(SessionStore())
            from ts_harness.agent.sessions import get_session_store

            session = get_session_store().get_or_create(
                tenant_id="default", source="web", agent_id="orchestrator"
            )
            path = Path(tmp) / "transcripts.jsonl"
            transcripts = TranscriptStore(path=path)
            for i in range(7):
                transcripts.append_messages(
                    session.session_id,
                    [{"role": "user", "content": f"needle recency note {i}"}],
                )

            hits = transcripts.search(tenant_id="default", query="needle", limit=3)

            self.assertEqual([hit.seq for hit in hits], [7, 6, 5])
            self.assertEqual(
                [hit.session_id for hit in hits],
                [session.session_id] * 3,
            )
            self.addCleanup(reset_session_store, SessionStore())

    def test_jsonl_search_redacts_credential_in_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_session_store(SessionStore())
            from ts_harness.agent.sessions import get_session_store

            session = get_session_store().get_or_create(
                tenant_id="default", source="slack", agent_id="ops-agent"
            )
            path = Path(tmp) / "transcripts.jsonl"
            transcripts = TranscriptStore(path=path)
            transcripts.append_messages(
                session.session_id,
                [
                    {
                        "role": "user",
                        "content": "token is Authorization: Bearer super-secret-token",
                    }
                ],
            )
            hits = transcripts.search(tenant_id="default", query="Authorization")
            self.assertEqual(len(hits), 1)
            self.assertNotIn("super-secret-token", hits[0].snippet)
            self.addCleanup(reset_session_store, SessionStore())

    @unittest.skipUnless(
        os.environ.get("TS_HARNESS_SESSION_DB_URL"),
        "TS_HARNESS_SESSION_DB_URL not set",
    )
    def test_postgres_round_trips_tool_calls(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg not installed")

        db_url = os.environ["TS_HARNESS_SESSION_DB_URL"]
        reset_session_store(SessionStore(db_url=db_url))
        self.addCleanup(reset_session_store, SessionStore())
        from ts_harness.agent.sessions import get_session_store

        session_id = get_session_store().get_or_create(tenant_id="default", source="web", agent_id="orchestrator").session_id
        transcripts = TranscriptStore(db_url=db_url)
        self.addCleanup(transcripts.clear, session_id)
        tool_calls = [{"id": "call-1", "type": "function", "function": {"name": "propose_start_program", "arguments": "{}"}}]
        transcripts.append_turn(
            session_id,
            user_text="write a PRD",
            assistant_messages=[
                {"role": "assistant", "content": None, "tool_calls": tool_calls},
                {"role": "tool", "tool_call_id": "call-1", "content": "{\"status\": \"pending\"}"},
                {"role": "assistant", "content": "Reply yes to start."},
            ],
        )

        loaded = transcripts.load_messages(session_id)

        self.assertEqual([msg["role"] for msg in loaded], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(loaded[1]["tool_calls"], tool_calls)
        self.assertEqual(loaded[2]["tool_call_id"], "call-1")

    @unittest.skipUnless(
        os.environ.get("TS_HARNESS_SESSION_DB_URL"),
        "TS_HARNESS_SESSION_DB_URL not set",
    )
    def test_postgres_search_tenant_scoped(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg not installed")

        db_url = os.environ["TS_HARNESS_SESSION_DB_URL"]
        reset_session_store(SessionStore(db_url=db_url))
        from ts_harness.agent.sessions import get_session_store

        store = get_session_store()
        tenant_a = store.get_or_create(
            tenant_id="tenant-a",
            source="web",
            agent_id="orchestrator",
        )
        tenant_b = store.get_or_create(
            tenant_id="tenant-b",
            source="web",
            agent_id="orchestrator",
        )
        transcripts = TranscriptStore(db_url=db_url)
        self.addCleanup(transcripts.clear, tenant_a.session_id)
        self.addCleanup(transcripts.clear, tenant_b.session_id)
        self.addCleanup(reset_session_store, SessionStore())
        transcripts.append_messages(
            tenant_a.session_id,
            [{"role": "user", "content": "checkout postgres tenant A"}],
        )
        transcripts.append_messages(
            tenant_b.session_id,
            [{"role": "user", "content": "checkout postgres tenant B"}],
        )

        hits = transcripts.search(
            tenant_id="tenant-a",
            query="checkout postgres",
            limit=10,
        )

        self.assertEqual([hit.session_id for hit in hits], [tenant_a.session_id])
        self.assertNotIn("<b>", hits[0].snippet)


if __name__ == "__main__":
    unittest.main()
