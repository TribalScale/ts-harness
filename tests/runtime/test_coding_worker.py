"""The coding worker: spec, heartbeat with the stream log's last line, result."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.coding import worker


class FakeApi:
    job_id = "job-1"

    def __init__(self, status="running", spec=None, heartbeat_status="running"):
        self.job = {"status": status, "spec": spec or {}}
        self.heartbeat_status = heartbeat_status
        self.heartbeats = []
        self.completed = []
        self.beat = threading.Event()
        self.registry_list = []

    def spec(self):
        return self.job

    def registries(self):
        return list(self.registry_list)

    def github_token(self):
        return None

    def heartbeat(self, last_output):
        self.heartbeats.append(last_output)
        self.beat.set()
        return self.heartbeat_status

    def complete(self, *, result=None, error=None):
        self.completed.append((result, error))
        return {}


class RunJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = {"workspace_dir": self.tmp.name, "work_order_id": "WO-1"}

    def test_reports_the_result(self):
        api = FakeApi(spec=self.spec)
        code = worker.run_job(api, run=lambda spec: {"ok": True, "pr_url": "https://x/pull/1"}, heartbeat_seconds=60)
        self.assertEqual(code, 0)
        self.assertEqual(api.completed, [({"ok": True, "pr_url": "https://x/pull/1"}, None)])

    def test_heartbeat_sends_the_last_log_line(self):
        log = Path(self.tmp.name) / ".ts-harness" / "opencode-WO-1.log"
        log.parent.mkdir(parents=True)
        log.write_text("first\nrunning npm test\n\n")
        api = FakeApi(spec=self.spec)

        def run(spec):
            api.beat.wait(2)
            return {"ok": True}

        worker.run_job(api, run=run, heartbeat_seconds=0.01)
        self.assertEqual(api.heartbeats[0], "running npm test")

    def test_a_crash_reports_an_error(self):
        api = FakeApi(spec=self.spec)

        def run(spec):
            raise RuntimeError("clone failed")

        self.assertEqual(worker.run_job(api, run=run, heartbeat_seconds=60), 1)
        self.assertEqual(api.completed, [(None, "coding worker error: clone failed")])

    def test_cancellation_stops_without_completing(self):
        api = FakeApi(spec=self.spec, heartbeat_status="cancelled")
        cancelled = threading.Event()

        def run(spec):
            cancelled.wait(2)
            return {"ok": True}

        code = worker.run_job(api, run=run, heartbeat_seconds=0.01, on_cancel=cancelled.set)
        self.assertEqual(code, 0)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(api.completed, [])

    def test_a_job_that_is_not_running_is_left_alone(self):
        api = FakeApi(status="cancelled")
        self.assertEqual(worker.run_job(api, run=lambda spec: self.fail("ran")), 0)
        self.assertEqual(api.completed, [])

    def test_main_requires_its_environment(self):
        self.assertEqual(worker.main({"JOB_ID": "j"}), 2)

    def test_default_run_leaves_ref_recording_to_the_memory_api(self):
        api = FakeApi(spec=self.spec)
        with mock.patch(
            "ts_harness.implementation.coding_job.run_coding_request", return_value={"ok": True}
        ) as run:
            self.assertEqual(worker.run_job(api, heartbeat_seconds=60), 0)
        run.assert_called_once_with(self.spec, record_refs=False)


class LastLogLineTests(unittest.TestCase):
    def test_missing_file(self):
        self.assertIsNone(worker.last_log_line(Path("/nonexistent/log")))


class JobApiTests(unittest.TestCase):
    def test_sends_the_job_token_to_the_jobs_own_endpoint(self):
        seen = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"status": "running"}).encode()

        def urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["auth"] = request.headers["Authorization"]
            seen["body"] = json.loads(request.data)
            return Response()

        api = worker.JobApi("http://memory/", "job-1", "cj1.tok")
        with mock.patch.object(worker.urllib.request, "urlopen", urlopen):
            self.assertEqual(api.heartbeat("line"), "running")
        self.assertEqual(seen, {"url": "http://memory/coding-jobs/job-1/heartbeat", "auth": "Bearer cj1.tok", "body": {"last_output": "line"}})


if __name__ == "__main__":
    unittest.main()
