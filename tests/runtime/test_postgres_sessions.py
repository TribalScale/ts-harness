"""Postgres session store integration (skipped unless DB URL is set)."""

from __future__ import annotations

import os
import unittest
import uuid

import pytest

from ts_harness.agent.sessions import SessionStore


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


if __name__ == "__main__":
    unittest.main()
