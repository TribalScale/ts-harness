"""LocalDockerLauncher commands and the launcher tick."""

from __future__ import annotations

import subprocess
import threading
import unittest

from ts_harness.coding import launcher as launcher_mod
from ts_harness.coding.launcher import LaunchError, LocalDockerConfig, LocalDockerLauncher, launcher_tick, worker_env_names

JOB = {"id": "6f1c1f9e-1111-4222-8333-444455556666"}


class FakeDocker:
    def __init__(self, *, run_rc=0, status="running"):
        self.calls = []
        self.run_rc = run_rc
        self.status = status

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if cmd[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(cmd, self.run_rc, stdout="abc123\n", stderr="no such image")
        if cmd[:2] == ["docker", "inspect"]:
            rc = 0 if self.status != "missing" else 1
            return subprocess.CompletedProcess(cmd, rc, stdout=f"{self.status}\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


ENV = {"GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk", "CODING_BACKEND_MODEL": "m", "SLACK_BOT_TOKEN": "xoxb", "CODING_WORKER_ENV": "EXTRA_KEY", "EXTRA_KEY": "v", "EMPTY_KEY": ""}


class LocalDockerLauncherTests(unittest.TestCase):
    def test_worker_env_is_an_allowlist_of_names(self):
        self.assertEqual(worker_env_names(ENV), ["ANTHROPIC_API_KEY", "CODING_BACKEND_MODEL", "EXTRA_KEY", "GITHUB_TOKEN"])

    def test_start_runs_a_limited_labelled_container_without_secrets_in_argv(self):
        docker = FakeDocker()
        cfg = LocalDockerConfig(image="worker:1", network="container:abc", config_host_path="/data/rt.yaml")
        ref = LocalDockerLauncher(cfg, run=docker, environ=ENV).start(JOB, "cj1.token")
        self.assertEqual(ref, "abc123")
        cmd, kwargs = docker.calls[0]
        joined = " ".join(cmd)
        for expected in (
            "--rm", "--label ts-harness.coding-job=" + JOB["id"], "--cpus 2", "--memory 4g", "--pids-limit 2048",
            "--network container:abc", "-v /data/rt.yaml:/config/ts-harness-runtime.yaml:ro", "-e JOB_TOKEN ", "-e GITHUB_TOKEN",
        ):
            self.assertIn(expected, joined)
        self.assertEqual(cmd[-1], "worker:1")
        self.assertNotIn("ghp_x", joined)
        self.assertNotIn("cj1.token", joined)
        self.assertNotIn("SLACK_BOT_TOKEN", joined)
        self.assertEqual(kwargs["env"]["JOB_TOKEN"], "cj1.token")

    def test_start_failure_raises_with_stderr(self):
        with self.assertRaisesRegex(LaunchError, "no such image"):
            LocalDockerLauncher(LocalDockerConfig(), run=FakeDocker(run_rc=1), environ={}).start(JOB, "t")

    def test_describe_and_stop(self):
        docker = FakeDocker(status="missing")
        launcher = LocalDockerLauncher(LocalDockerConfig(), run=docker, environ={})
        self.assertEqual(launcher.describe("abc"), "missing")
        launcher.stop("abc")
        self.assertEqual(docker.calls[-1][0], ["docker", "stop", "-t", "10", "abc"])

    def test_config_from_env_resolves_auto_network(self):
        cfg = LocalDockerConfig.from_env({"CODING_WORKER_NETWORK": "auto", "CODING_WORKER_IMAGE": "img", "MEMORY_API_URL": "http://m"})
        self.assertTrue(cfg.network.startswith("container:"))
        self.assertEqual((cfg.image, cfg.memory_api_url), ("img", "http://m"))

    def test_build_launcher_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            launcher_mod.build_launcher({"CODING_LAUNCHER": "k8s"})


class FakeMemory:
    def __init__(self, jobs=None, finished=None):
        self.jobs = jobs or []
        self.finished = finished or []
        self.launches = []
        self.claims = []
        self.lease_calls = 0

    def claim_coding_jobs(self, *, launcher, max_running):
        self.claims.append((launcher, max_running))
        jobs, self.jobs = self.jobs, []
        return {"jobs": jobs}

    def record_coding_job_launch(self, job_id, *, runner_ref="", error="", retryable=False):
        self.launches.append((job_id, runner_ref, error) + (("retry",) if retryable else ()))

    def list_coding_jobs(self, **kwargs):
        self.list_kwargs = kwargs
        return self.finished

    def acquire_service_lease(self, name, *, holder, ttl_seconds):
        self.lease_calls += 1
        return {"leader": True}


class FakeLauncher:
    name = "local"

    def __init__(self, fail_ids=(), running=()):
        self.fail_ids = set(fail_ids)
        self.running = set(running)
        self.started = []
        self.stopped = []

    def start(self, job, token):
        if job["id"] in self.fail_ids:
            raise LaunchError("boom")
        self.started.append((job["id"], token))
        return f"c-{job['id']}"

    def stop(self, ref):
        self.stopped.append(ref)

    def describe(self, ref):
        return "running" if ref in self.running else "exited"


class LauncherTickTests(unittest.TestCase):
    def test_starts_jobs_records_refs_and_reports_failures(self):
        memory = FakeMemory(jobs=[{"id": "a", "token": "ta"}, {"id": "b", "token": "tb"}])
        launcher = FakeLauncher(fail_ids={"b"})
        result = launcher_tick(memory, launcher, max_running=3)
        self.assertEqual(result["started"], ["a"])
        self.assertEqual(result["failed"], ["b"])
        self.assertEqual(launcher.started, [("a", "ta")])
        self.assertEqual(memory.launches, [("a", "c-a", ""), ("b", "", "boom")])
        self.assertEqual(memory.claims, [("local", 3)])

    def test_stops_running_workers_of_cancelled_or_lost_jobs(self):
        finished = [
            {"id": "x", "status": "cancelled", "runner_ref": "c-x", "launcher": "local"},
            {"id": "y", "status": "lost", "runner_ref": "c-y", "launcher": "local"},
            {"id": "z", "status": "lost", "runner_ref": "c-z", "launcher": "ecs"},
            {"id": "w", "status": "cancelled", "runner_ref": "", "launcher": "local"},
        ]
        memory = FakeMemory(finished=finished)
        launcher = FakeLauncher(running={"c-x", "c-z"})
        result = launcher_tick(memory, launcher, max_running=3)
        self.assertEqual(result["stopped"], ["x"])
        self.assertEqual(launcher.stopped, ["c-x"])
        self.assertEqual(memory.list_kwargs["status"], "cancelled,lost")

    def test_loop_ticks_only_while_holding_the_lease(self):
        memory = FakeMemory()
        memory.acquire_service_lease = lambda name, **kw: {"leader": False}
        stop = threading.Event()
        ticks = []
        original = launcher_mod.launcher_tick
        launcher_mod.launcher_tick = lambda *a, **k: ticks.append(1) or {}
        try:
            stop.set()
            launcher_mod.run_launcher_loop(memory, FakeLauncher(), max_running=1, holder="h", stop=stop)
            stop.clear()
            calls = []

            def lease(name, **kw):
                calls.append(name)
                stop.set()
                return {"leader": True}

            memory.acquire_service_lease = lease
            launcher_mod.run_launcher_loop(memory, FakeLauncher(), max_running=1, holder="h", stop=stop, interval_seconds=0)
        finally:
            launcher_mod.launcher_tick = original
        self.assertEqual(calls, ["coding-launcher"])
        self.assertEqual(ticks, [1])


if __name__ == "__main__":
    unittest.main()
