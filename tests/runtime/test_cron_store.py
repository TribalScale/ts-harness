"""Tests for cron file store and scheduler hot-reload."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.cron_store import (
    load_cron_file,
    save_cron_file,
    validate_cron_job,
)
from ts_harness.gateway.scheduler import Scheduler

REPO_ROOT = Path(__file__).resolve().parents[2]


class CronStoreTests(unittest.TestCase):
    def test_validate_and_persist_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime-cron-jobs.json"
            job = validate_cron_job(
                {
                    "name": "weekly-ops",
                    "agent_id": "ops-agent",
                    "cron": "0 10 * * 1",
                    "prompt": "health",
                    "skill_id": "ops/health-check",
                }
            )
            save_cron_file([job], path)
            loaded = load_cron_file(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["name"], "weekly-ops")
            self.assertEqual(loaded[0]["skill_hints"], ["ops/health-check"])

    def test_replace_cron_jobs_preserves_heartbeats(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched: list[dict] = []
        scheduler = Scheduler(
            config,
            lambda payload: dispatched.append(payload),
            cron_jobs=[{"name": "a", "agent_id": "ops-agent", "prompt": "a", "interval_seconds": 60}],
        )
        heartbeat_count = sum(1 for j in scheduler.jobs if j.kind == "heartbeat")
        scheduler.replace_cron_jobs(
            [{"name": "b", "agent_id": "ops-agent", "prompt": "b", "interval_seconds": 60}]
        )
        self.assertEqual(sum(1 for j in scheduler.jobs if j.kind == "heartbeat"), heartbeat_count)
        self.assertTrue(any(j.name == "b" for j in scheduler.jobs))
        self.assertFalse(any(j.name == "a" and j.kind == "cron" for j in scheduler.jobs))
        listed = scheduler.list_cron_jobs()
        self.assertEqual(listed[0]["name"], "b")
        result = scheduler.trigger_job("b")
        self.assertTrue(result["dispatched"])
        self.assertTrue(any(p.get("name") == "b" for p in dispatched))

    def test_invalid_cron_rejected(self):
        with self.assertRaises(ValueError):
            validate_cron_job(
                {"name": "bad", "agent_id": "ops-agent", "cron": "not-a-cron", "prompt": "x"}
            )


if __name__ == "__main__":
    unittest.main()
