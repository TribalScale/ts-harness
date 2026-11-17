"""Postgres session store integration (skipped unless DB URL is set)."""

from __future__ import annotations

import os
import unittest
import uuid

import pytest

from ts_harness.agent.sessions import SessionRecord, SessionStore


@pytest.mark.skipif(
    not os.environ.get("TS_HARNESS_SESSION_DB_URL"),
    reason="TS_HARNESS_SESSION_DB_URL not set",
)
class PostgresSessionTests(unittest.TestCase):
    def test_session_persists_and_reloads_by_routing_key(self):
        db_url = os.environ["TS_HARNESS_SESSION_DB_URL"]
        thread_id = f"pg-test-{uuid.uuid4().hex[:8]}"
        store = SessionStore(db_url=db_url)
        created = store.get_or_create(
            tenant_id="default",
            source="workflow",
            agent_id="orchestrator",
            workflow_id=thread_id,
        )
        created.tool_count = 3
        store.save(created)

        reloaded = SessionStore(db_url=db_url)
        session = reloaded.get_or_create(
            tenant_id="default",
            source="workflow",
            agent_id="orchestrator",
            workflow_id=thread_id,
        )
        self.assertEqual(session.session_id, created.session_id)
        self.assertEqual(session.tool_count, 3)


class SessionStoreReadLoadsPostgresTests(unittest.TestCase):
    def test_list_and_get_load_postgres_before_reading_memory(self):
        store = SessionStore(db_url="postgresql://example")
        calls = {"n": 0}

        def fake_load() -> None:
            calls["n"] += 1
            session = SessionRecord(
                session_id="sess-1",
                routing_key="web:abc",
                tenant_id="default",
                agent_id="orchestrator",
                source="web",
            )
            store._by_id[session.session_id] = session
            store._by_routing[session.routing_key] = session

        store._load_postgres = fake_load  # type: ignore[method-assign]

        listed = store.list_for_tenant("default")
        self.assertEqual(calls["n"], 1)
        self.assertEqual([session.session_id for session in listed], ["sess-1"])
        store.list_for_tenant("default")
        self.assertEqual(calls["n"], 1)

        fresh = SessionStore(db_url="postgresql://example")
        fresh._load_postgres = fake_load  # type: ignore[method-assign]
        # Bind the loader to the fresh store's maps.
        def load_into_fresh() -> None:
            calls["n"] += 1
            session = SessionRecord(
                session_id="sess-1",
                routing_key="web:abc",
                tenant_id="default",
                agent_id="orchestrator",
                source="web",
            )
            fresh._by_id[session.session_id] = session
            fresh._by_routing[session.routing_key] = session

        fresh._load_postgres = load_into_fresh  # type: ignore[method-assign]
        self.assertEqual(fresh.get("sess-1").session_id, "sess-1")
        self.assertEqual(calls["n"], 2)
        self.assertIsNone(fresh.get("missing"))
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
