"""Usage rows, the monthly rollup and shared rate buckets in Postgres (migration 040)."""

from __future__ import annotations

import unittest

from tests.pg_support import PgTestCase, requires_pg

from ts_harness.llm_proxy.metering import Usage
from ts_harness.llm_proxy.store import ModelRateLimiter, UsageStore


@requires_pg
class UsageStoreTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM llm_usage")
            conn.execute("DELETE FROM llm_rate_buckets")
            conn.commit()
        self.store = UsageStore(self.db.connect)

    def rows(self):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM llm_usage ORDER BY id").fetchall()

    def test_records_tokens_cost_and_scope(self):
        self.store.record(
            {"tenant": "acme", "team": "t-1", "project": "web", "user": "dev@acme.test", "agent": "dev-agent"},
            provider="openai",
            model="gpt-4o",
            usage=Usage(input_tokens=100, output_tokens=20, cache_read_tokens=50),
            cost_usd=0.0123,
            status=200,
        )
        [row] = self.rows()
        self.assertEqual(
            (row["tenant_id"], row["team_id"], row["project_id"], row["actor"], row["provider"], row["model"]),
            ("acme", "t-1", "web", "dev@acme.test", "openai", "gpt-4o"),
        )
        self.assertEqual((row["input_tokens"], row["output_tokens"], float(row["cost_usd"]), row["status"]), (150, 20, 0.0123, 200))

    def test_team_and_program_fall_back_to_the_task_and_workflow(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO teams (slug, display_name) VALUES ('usage-team', 'U') RETURNING id")
            team = str(cur.fetchone()["id"])
            cur.execute(
                "INSERT INTO delivery_programs (summary, entry_phase, current_phase) VALUES ('p', 'prd', 'prd') RETURNING id"
            )
            program = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, program_id) VALUES ('t', %s) RETURNING id", (program,))
            parent = str(cur.fetchone()["id"])
            cur.execute("INSERT INTO workflows (workflow_type, parent_workflow_id) VALUES ('t', %s) RETURNING id", (parent,))
            child = str(cur.fetchone()["id"])
            cur.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, team_id) VALUES (%s, 's', 'dev-agent', %s) RETURNING id",
                (child, team),
            )
            task = str(cur.fetchone()["id"])
            conn.commit()
        self.store.record({"workflow": child, "task": task}, provider="openai", model="m", usage=Usage(), cost_usd=0, status=200)
        [row] = self.rows()
        self.assertEqual((row["team_id"], row["program_id"], row["tenant_id"]), (team, program, "default"))

    def test_ids_that_are_not_uuids_are_stored_without_lookups(self):
        self.store.record({"workflow": "wf-local", "task": "task-local"}, provider="x", model="m", usage=Usage(), cost_usd=0, status=500)
        [row] = self.rows()
        self.assertEqual((row["workflow_id"], row["task_id"], row["team_id"], row["program_id"]), ("wf-local", "task-local", None, None))

    def test_monthly_view_sums_per_team_and_model(self):
        for cost in (1.5, 2.25):
            self.store.record({"team": "t-1"}, provider="openai", model="gpt-4o", usage=Usage(10, 5), cost_usd=cost, status=200)
        self.store.record({"team": "t-2"}, provider="openai", model="gpt-4o", usage=Usage(1, 1), cost_usd=9, status=200)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM llm_usage_monthly WHERE team_id = 't-1' AND month = date_trunc('month', now())::date"
            ).fetchone()
        self.assertEqual((row["calls"], row["input_tokens"], row["output_tokens"], float(row["cost_usd"])), (2, 20, 10, 3.75))


@requires_pg
class ModelRateLimiterTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM llm_rate_buckets")
            conn.commit()

    def test_burst_then_refuse_and_buckets_are_shared(self):
        limits = {"anthropic/*": {"requests_per_minute": 1, "burst": 2}}
        first, second = ModelRateLimiter(self.db.connect, limits), ModelRateLimiter(self.db.connect, limits)
        self.assertEqual([first.allow("anthropic", "claude"), second.allow("anthropic", "claude")], [True, True])
        self.assertFalse(first.allow("anthropic", "claude"))
        self.assertTrue(first.allow("openai", "gpt-4o"))

    def test_bucket_refills_with_time(self):
        limiter = ModelRateLimiter(self.db.connect, {"*": {"requests_per_minute": 60, "burst": 1}})
        self.assertTrue(limiter.allow("openai", "m"))
        self.assertFalse(limiter.allow("openai", "m"))
        with self.db.connect() as conn:
            conn.execute("UPDATE llm_rate_buckets SET updated_at = now() - interval '2 seconds'")
            conn.commit()
        self.assertTrue(limiter.allow("openai", "m"))


if __name__ == "__main__":
    unittest.main()
