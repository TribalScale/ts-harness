"""Coding job storage, worker tokens and the task resume on completion."""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import coding_jobs  # noqa: E402

SPEC = {"spec_version": 1, "repo_url": "https://github.com/acme/app", "base_branch": "main", "work_order_id": "WO-1"}


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_cj", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JobTokenTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret", "MEMORY_API_TOKEN": "api"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_token_is_scoped_to_one_job_and_expires(self):
        token = coding_jobs.issue_job_token("job-a", ttl_seconds=60, now=1000)
        self.assertTrue(coding_jobs.verify_job_token(token, "job-a", now=1030))
        self.assertFalse(coding_jobs.verify_job_token(token, "job-b", now=1030))
        self.assertFalse(coding_jobs.verify_job_token(token, "job-a", now=1061))
        self.assertFalse(coding_jobs.verify_job_token(token[:-1] + ("0" if token[-1] != "0" else "1"), "job-a", now=1030))
        self.assertFalse(coding_jobs.verify_job_token("garbage", "job-a"))

    def test_secret_falls_back_to_a_key_derived_from_the_api_token(self):
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": ""}):
            token = coding_jobs.issue_job_token("job-a")
            self.assertTrue(coding_jobs.verify_job_token(token, "job-a"))
        self.assertFalse(coding_jobs.verify_job_token(token, "job-a"))

    def test_missing_secrets_raise(self):
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "", "MEMORY_API_TOKEN": ""}):
            with self.assertRaises(RuntimeError):
                coding_jobs.issue_job_token("job-a")


class WorkerAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = load_gateway()

    def handler(self, path, token):
        h = object.__new__(self.gateway.MemoryHandler)
        h.path = path
        h.headers = {"Authorization": f"Bearer {token}"}
        return h

    def test_job_token_opens_only_its_worker_endpoints(self):
        job = "6f1c1f9e-1111-4222-8333-444455556666"
        other = "6f1c1f9e-1111-4222-8333-777788889999"
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret", "MEMORY_API_TOKEN": "api"}):
            token = coding_jobs.issue_job_token(job)
            self.assertTrue(self.handler(f"/coding-jobs/{job}/spec", token)._coding_job_worker_authorized("GET"))
            self.assertTrue(self.handler(f"/coding-jobs/{job}/heartbeat", token)._coding_job_worker_authorized("POST"))
            self.assertTrue(self.handler(f"/coding-jobs/{job}/complete", token)._coding_job_worker_authorized("POST"))
            self.assertFalse(self.handler(f"/coding-jobs/{other}/complete", token)._coding_job_worker_authorized("POST"))
            self.assertFalse(self.handler(f"/coding-jobs/{job}/consume", token)._coding_job_worker_authorized("POST"))
            self.assertFalse(self.handler(f"/coding-jobs/{job}/spec", token)._coding_job_worker_authorized("POST"))
            self.assertFalse(self.handler("/coding-jobs/claim", token)._coding_job_worker_authorized("POST"))
            self.assertFalse(self.handler("/memory/recall", token)._coding_job_worker_authorized("POST"))
            self.assertFalse(self.handler(f"/coding-jobs/{job}/spec", "api")._coding_job_worker_authorized("GET"))
            self.assertFalse(self.handler(f"/coding-jobs/{job}/spec", token)._authorized())


class ValidationTests(unittest.TestCase):
    def test_create_requires_spec_with_repo(self):
        with self.assertRaises(ValueError):
            coding_jobs.create_coding_job(mock.MagicMock(), {"spec": {"work_order_id": "WO-1"}})

    def test_cancel_requires_a_target(self):
        with self.assertRaises(ValueError):
            coding_jobs.cancel_coding_jobs(mock.MagicMock(), {})

    def test_bad_job_id(self):
        with self.assertRaises(ValueError):
            coding_jobs.heartbeat_coding_job(mock.MagicMock(), "nope", {})


@requires_pg
class CodingJobsPostgresTests(PgTestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM coding_jobs")
            conn.commit()

    def parked_task(self, conn):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workflows (workflow_type) VALUES ('deliver_work_order') RETURNING id")
            workflow_id = cur.fetchone()["id"]
            cur.execute(
                """
                INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, error, attempt, wait_token, expected_event_type)
                VALUES (%s, 'deliver_work_order', 'dev-agent', 'blocked', %s, 1, 'tok', 'coding.finished')
                RETURNING id
                """,
                (workflow_id, coding_jobs.WAIT_ERROR),
            )
            task_id = cur.fetchone()["id"]
        conn.commit()
        return str(workflow_id), str(task_id)

    def task(self, conn, task_id):
        with conn.cursor() as cur:
            cur.execute("SELECT status, error, attempt, wait_token FROM workflow_tasks WHERE id = %s", (task_id,))
            return cur.fetchone()

    def create(self, conn, workflow_id=None, task_id=None):
        return coding_jobs.create_coding_job(conn, {"spec": SPEC, "workflow_id": workflow_id, "task_id": task_id})

    def test_full_lifecycle_resumes_the_parked_task(self):
        with self.db.connect() as conn:
            workflow_id, task_id = self.parked_task(conn)
            job = self.create(conn, workflow_id, task_id)
            self.assertTrue(job["created"])
            self.assertEqual(self.create(conn, workflow_id, task_id)["id"], job["id"])

            claimed = coding_jobs.claim_coding_jobs(conn, {"launcher": "local", "max_running": 2})["jobs"]
            self.assertEqual([j["id"] for j in claimed], [job["id"]])
            self.assertTrue(coding_jobs.verify_job_token(claimed[0]["token"], job["id"]))
            coding_jobs.record_launch(conn, job["id"], {"runner_ref": "container-1"})

            spec = coding_jobs.get_job_spec(conn, job["id"])
            self.assertEqual((spec["status"], spec["spec"]["repo_url"]), ("running", SPEC["repo_url"]))
            self.assertEqual(coding_jobs.heartbeat_coding_job(conn, job["id"], {"last_output": "x" * 5000})["status"], "running")

            done = coding_jobs.complete_coding_job(
                conn, job["id"], {"result": {"ok": True, "pr_url": "https://github.com/acme/app/pull/1"}}
            )
            self.assertEqual((done["status"], done["task_resumed"]), ("succeeded", True))
            task = self.task(conn, task_id)
            self.assertEqual((task["status"], task["error"], task["attempt"], task["wait_token"]), ("pending", None, 0, None))

            again = coding_jobs.complete_coding_job(conn, job["id"], {"result": {"ok": False}})
            self.assertTrue(again["ignored"])
            jobs = coding_jobs.list_coding_jobs(conn, {"task_id": task_id, "unconsumed": "true"})["jobs"]
            self.assertEqual(len(jobs[0]["last_output"]), 2000)
            coding_jobs.consume_coding_job(conn, job["id"])
            self.assertEqual(coding_jobs.list_coding_jobs(conn, {"task_id": task_id, "unconsumed": "1"})["jobs"], [])

            with conn.cursor() as cur:
                cur.execute("SELECT action_type FROM session_audit_log WHERE session_id = %s ORDER BY occurred_at", (workflow_id,))
                actions = [r["action_type"] for r in cur.fetchall()]
            self.assertEqual(actions, ["coding_job_created", "coding_job_started", "coding_job_finished"])

    def test_failed_result_and_missing_result(self):
        with self.db.connect() as conn:
            job = self.create(conn)
            coding_jobs.claim_coding_jobs(conn, {"max_running": 5})
            done = coding_jobs.complete_coding_job(conn, job["id"], {"error": "worker crashed"})
            self.assertEqual(done["status"], "failed")
            self.assertEqual(done["result"], {"ok": False, "error": "worker crashed"})
            self.assertFalse(done["task_resumed"])

    def test_claim_respects_the_running_cap(self):
        with self.db.connect() as conn:
            ids = [self.create(conn)["id"] for _ in range(4)]
            first = coding_jobs.claim_coding_jobs(conn, {"max_running": 3, "limit": 2})
            self.assertEqual([j["id"] for j in first["jobs"]], ids[:2])
            second = coding_jobs.claim_coding_jobs(conn, {"max_running": 3})
            self.assertEqual(([j["id"] for j in second["jobs"]], second["running"]), (ids[2:3], 2))
            self.assertEqual(coding_jobs.claim_coding_jobs(conn, {"max_running": 3})["jobs"], [])

    def test_claim_applies_user_program_and_team_quotas(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO teams (slug, display_name) VALUES ('quota-team', 'Q') RETURNING id")
                team = str(cur.fetchone()["id"])
            conn.commit()

            def job(requester, program=None, team_id=None):
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO workflows (workflow_type) VALUES ('deliver_work_order') RETURNING id")
                    wf = cur.fetchone()["id"]
                    cur.execute(
                        "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, requested_by, team_id) VALUES (%s, 'd', 'dev-agent', %s, %s) RETURNING id",
                        (wf, requester, team_id),
                    )
                    task = str(cur.fetchone()["id"])
                conn.commit()
                body = {"spec": SPEC, "workflow_id": str(wf), "task_id": task}
                if program:
                    body["program_id"] = program
                return coding_jobs.create_coding_job(conn, body)["id"]

            program = "6f1c1f9e-1111-4222-8333-000000000001"
            alice = [job("alice"), job("alice"), job("alice")]
            bob = [job("bob", program), job("bob", program)]
            team_jobs = [job("carol", team_id=team), job("dave", team_id=team)]
            quotas = {"per_user": 2, "per_program": 1, "per_team": 1}
            claimed = coding_jobs.claim_coding_jobs(conn, {"max_running": 10, "quotas": quotas})
            self.assertEqual({j["id"] for j in claimed["jobs"]}, {alice[0], alice[1], bob[0], team_jobs[0]})
            self.assertEqual(claimed["deferred_by_quota"], 3)
            again = coding_jobs.claim_coding_jobs(conn, {"max_running": 10, "quotas": quotas})
            self.assertEqual(again["jobs"], [])
            with self.assertRaisesRegex(ValueError, "per_user"):
                coding_jobs.claim_coding_jobs(conn, {"quotas": {"per_user": "x"}})

    def test_concurrent_claims_never_exceed_the_cap(self):
        with self.db.connect() as conn:
            for _ in range(6):
                self.create(conn)
        results: list[list[str]] = []
        barrier = threading.Barrier(4)

        def claim():
            with self.db.connect() as conn:
                barrier.wait()
                results.append([j["id"] for j in coding_jobs.claim_coding_jobs(conn, {"max_running": 3})["jobs"]])

        threads = [threading.Thread(target=claim) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        claimed = [j for batch in results for j in batch]
        self.assertEqual(len(claimed), 3)
        self.assertEqual(len(set(claimed)), 3)

    def test_launch_failures_requeue_then_fail_and_resume(self):
        with self.db.connect() as conn:
            workflow_id, task_id = self.parked_task(conn)
            job = self.create(conn, workflow_id, task_id)
            for attempt in range(1, coding_jobs.MAX_LAUNCH_ATTEMPTS + 1):
                coding_jobs.claim_coding_jobs(conn, {"max_running": 1})
                row = coding_jobs.record_launch(conn, job["id"], {"error": "no capacity"})
                expected = "queued" if attempt < coding_jobs.MAX_LAUNCH_ATTEMPTS else "failed"
                self.assertEqual(row["status"], expected)
            self.assertEqual(self.task(conn, task_id)["status"], "pending")

    def test_capacity_waits_do_not_spend_launch_attempts(self):
        with self.db.connect() as conn:
            job = self.create(conn)
            for _ in range(coding_jobs.MAX_LAUNCH_ATTEMPTS + 2):
                coding_jobs.claim_coding_jobs(conn, {"max_running": 1})
                row = coding_jobs.record_launch(conn, job["id"], {"error": "vCPU limit", "retryable": True})
                self.assertEqual((row["status"], row["attempt"]), ("queued", 0))
            self.assertIn("waiting for capacity", row["error"])

    def test_completion_records_branch_and_pr_refs_for_the_program(self):
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO delivery_programs (summary, entry_phase, current_phase)
                    VALUES ('refs', 'deliver', 'deliver') RETURNING id
                    """
                )
                program_id = str(cur.fetchone()["id"])
            conn.commit()
            workflow_id, task_id = self.parked_task(conn)
            job = coding_jobs.create_coding_job(
                conn,
                {"spec": {**SPEC, "record_workflow_id": workflow_id}, "workflow_id": workflow_id,
                 "task_id": task_id, "program_id": program_id},
            )
            coding_jobs.claim_coding_jobs(conn, {"max_running": 1})
            done = coding_jobs.complete_coding_job(
                conn,
                job["id"],
                {"result": {"ok": True, "branch": "ts/wo-1", "pr_url": "https://github.com/acme/app/pull/7",
                            "repo_url": "https://github.com/Acme/App.git", "work_order_id": "WO-1"}},
            )
            self.assertEqual(done["refs_recorded"], ["acme/app#ts/wo-1", "https://github.com/acme/app/pull/7"])
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT kind, ref, workflow_id, metadata FROM program_external_refs WHERE program_id = %s ORDER BY kind",
                    (program_id,),
                )
                rows = cur.fetchall()
            self.assertEqual([(r["kind"], r["ref"]) for r in rows],
                             [("branch", "acme/app#ts/wo-1"), ("pull_request", "https://github.com/acme/app/pull/7")])
            self.assertEqual(str(rows[0]["workflow_id"]), workflow_id)
            self.assertEqual(rows[0]["metadata"]["coding_job_id"], job["id"])

    def test_stale_jobs_are_lost_and_resume_the_task(self):
        with self.db.connect() as conn:
            workflow_id, task_id = self.parked_task(conn)
            job = self.create(conn, workflow_id, task_id)
            coding_jobs.claim_coding_jobs(conn, {"max_running": 1})
            coding_jobs.get_job_spec(conn, job["id"])
            self.assertEqual(coding_jobs.sweep_stale_coding_jobs(conn, {"heartbeat_seconds": 300})["lost"], [])
            conn.execute("UPDATE coding_jobs SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s", (job["id"],))
            conn.commit()
            lost = coding_jobs.sweep_stale_coding_jobs(conn, {"heartbeat_seconds": 300})["lost"]
            self.assertEqual([j["id"] for j in lost], [job["id"]])
            self.assertIn("no heartbeat", lost[0]["error"])
            self.assertFalse(lost[0]["result"]["ok"])
            self.assertEqual(self.task(conn, task_id)["status"], "pending")

    def test_sweep_resumes_a_task_parked_after_its_job_ended(self):
        with self.db.connect() as conn:
            workflow_id, task_id = self.parked_task(conn)
            conn.execute("UPDATE workflow_tasks SET status = 'running', error = NULL WHERE id = %s", (task_id,))
            conn.commit()
            job = self.create(conn, workflow_id, task_id)
            coding_jobs.claim_coding_jobs(conn, {"max_running": 1})
            self.assertFalse(coding_jobs.complete_coding_job(conn, job["id"], {"result": {"ok": True}})["task_resumed"])
            conn.execute("UPDATE workflow_tasks SET status = 'blocked', error = %s WHERE id = %s", (coding_jobs.WAIT_ERROR, task_id))
            conn.commit()
            self.assertEqual(coding_jobs.sweep_stale_coding_jobs(conn, {})["resumed_tasks"], [task_id])
            self.assertEqual(self.task(conn, task_id)["status"], "pending")

    def test_overtime_jobs_are_lost(self):
        with self.db.connect() as conn:
            job = self.create(conn)
            coding_jobs.claim_coding_jobs(conn, {"max_running": 1})
            coding_jobs.get_job_spec(conn, job["id"])
            conn.execute("UPDATE coding_jobs SET started_at = now() - interval '2 hours' WHERE id = %s", (job["id"],))
            conn.commit()
            lost = coding_jobs.sweep_stale_coding_jobs(conn, {"max_runtime_seconds": 3600})["lost"]
            self.assertIn("ran longer than 60 minutes", lost[0]["error"])

    def test_cancel_by_workflow_and_heartbeat_reports_it(self):
        with self.db.connect() as conn:
            workflow_id, task_id = self.parked_task(conn)
            job = self.create(conn, workflow_id, task_id)
            coding_jobs.claim_coding_jobs(conn, {"max_running": 1})
            coding_jobs.record_launch(conn, job["id"], {"runner_ref": "c-9"})
            cancelled = coding_jobs.cancel_coding_jobs(conn, {"workflow_ids": [workflow_id], "reason": "workflow cancelled"})
            self.assertEqual([(j["id"], j["runner_ref"]) for j in cancelled["cancelled"]], [(job["id"], "c-9")])
            self.assertEqual(coding_jobs.heartbeat_coding_job(conn, job["id"], {})["status"], "cancelled")
            self.assertTrue(coding_jobs.complete_coding_job(conn, job["id"], {"result": {"ok": True}})["ignored"])
            self.assertEqual(self.task(conn, task_id)["status"], "blocked")

    def test_unknown_job(self):
        with self.db.connect() as conn:
            with self.assertRaises(LookupError):
                coding_jobs.get_job_spec(conn, "6f1c1f9e-1111-4222-8333-444455556666")


if __name__ == "__main__":
    unittest.main()
