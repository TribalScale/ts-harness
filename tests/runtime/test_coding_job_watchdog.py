"""The sweep_coding_jobs system job, workflow cancel and the launcher lease."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg
from ts_harness.gateway import system_jobs

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import coding_jobs, leases  # noqa: E402


class SweepSystemJobTests(unittest.TestCase):
    def test_sweeps_with_configured_limits(self):
        client = mock.MagicMock()
        client.sweep_stale_coding_jobs.return_value = {"lost": [{"id": "job-1"}], "resumed_tasks": ["t"]}
        with mock.patch("ts_harness.memory.client.get_memory_client", return_value=client), mock.patch.dict(
            os.environ, {"CODING_JOB_HEARTBEAT_TIMEOUT_SECONDS": "120", "CODING_JOB_MAX_RUNTIME_SECONDS": "1800"}
        ):
            result = system_jobs.dispatch_system_job({"handler": "sweep_coding_jobs"})
        self.assertEqual(result, {"status": "ok", "lost": ["job-1"], "resumed_tasks": ["t"]})
        client.sweep_stale_coding_jobs.assert_called_once_with(heartbeat_seconds=120, max_runtime_seconds=1800)

    def test_errors_are_reported_not_raised(self):
        client = mock.MagicMock()
        client.sweep_stale_coding_jobs.side_effect = RuntimeError("memory api down")
        with mock.patch("ts_harness.memory.client.get_memory_client", return_value=client):
            self.assertEqual(system_jobs.sweep_coding_jobs()["status"], "error")


@requires_pg
class WatchdogPostgresTests(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import importlib.util

        spec = importlib.util.spec_from_file_location("memory_gateway_wd", REPO_ROOT / "scripts" / "memory-gateway.py")
        cls.gateway = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gateway)

    def test_cancelling_a_workflow_cancels_its_coding_jobs(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO workflows (workflow_type) VALUES ('deliver_work_order') RETURNING id")
                workflow_id = str(cur.fetchone()["id"])
            conn.commit()
            job = coding_jobs.create_coding_job(conn, {"spec": {"repo_url": "https://github.com/a/b"}, "workflow_id": workflow_id})
            self.gateway.cancel_workflow_bulk(conn, workflow_id, {"reason": "program cancelled"})
            row = coding_jobs.list_coding_jobs(conn, {"workflow_id": workflow_id})["jobs"][0]
            self.assertEqual((row["id"], row["status"], row["error"]), (job["id"], "cancelled", "program cancelled"))
            self.assertEqual(
                [j["id"] for j in coding_jobs.list_coding_jobs(conn, {"status": "cancelled", "finished_within": "60"})["jobs"]],
                [job["id"]],
            )

    def test_service_lease_has_one_holder_until_it_expires(self):
        with self.db.connect() as conn:
            self.assertTrue(leases.acquire_service_lease(conn, {"name": "coding-launcher", "holder": "a", "ttl_seconds": 30})["leader"])
            other = leases.acquire_service_lease(conn, {"name": "coding-launcher", "holder": "b"})
            self.assertEqual(other, {"leader": False, "holder": "a"})
            self.assertTrue(leases.acquire_service_lease(conn, {"name": "coding-launcher", "holder": "a"})["leader"])
            conn.execute("UPDATE service_leases SET expires_at = now() - interval '1 second'")
            conn.commit()
            self.assertTrue(leases.acquire_service_lease(conn, {"name": "coding-launcher", "holder": "b"})["leader"])
            self.assertTrue(leases.acquire_service_lease(conn, {"name": "other", "holder": "a"})["leader"])


if __name__ == "__main__":
    unittest.main()
