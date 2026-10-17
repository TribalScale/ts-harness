import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.scheduler import ScheduledJob, Scheduler, _within_active_hours


REPO_ROOT = Path(__file__).resolve().parents[2]


class SchedulerTests(unittest.TestCase):
    def test_active_hours_honor_job_timezone(self):
        job = ScheduledJob(
            agent_id="ops-agent",
            kind="heartbeat",
            interval_seconds=60,
            prompt="check",
            active_start="09:00",
            active_end="10:00",
            timezone="America/Toronto",
        )
        utc_time = datetime.fromisoformat("2026-01-01T14:30:00+00:00")

        self.assertTrue(_within_active_hours(job, utc_time))

    def test_empty_heartbeat_file_skips_dispatch(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched = []
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = Path(tmp) / "orchestrator" / "HEARTBEAT.md"
            heartbeat.parent.mkdir(parents=True)
            heartbeat.write_text("   \n")
            scheduler = Scheduler(config, lambda payload: dispatched.append(payload), workspace_root=Path(tmp))

            scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)

        self.assertEqual(dispatched, [])

    def test_cron_jobs_dispatch_on_separate_lane_without_blocking(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched = []
        scheduler = Scheduler(
            config,
            lambda payload: dispatched.append(payload),
            cron_jobs=[{"name": "memory-maintenance", "agent_id": "orchestrator", "prompt": "maintain"}],
        )

        scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)

        self.assertTrue(any(payload["source"] == "cron" for payload in dispatched))
        self.assertTrue(any(payload["lane"] == "cron" for payload in dispatched))

    def test_cron_job_includes_announce_target_when_configured(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched = []
        scheduler = Scheduler(
            config,
            lambda payload: dispatched.append(payload),
            cron_jobs=[
                {
                    "name": "standup",
                    "agent_id": "comms-agent",
                    "prompt": "standup",
                    "announce": {"channel": "slack", "to": "channel:C123"},
                }
            ],
        )

        scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)

        cron_payload = next(payload for payload in dispatched if payload["agent_id"] == "comms-agent" and payload["kind"] == "cron")
        self.assertEqual(cron_payload["announce"], {"channel": "slack", "to": "channel:C123"})

    def test_system_job_dispatches_handler_without_agent_prompt_lane(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched = []
        scheduler = Scheduler(
            config,
            lambda payload: dispatched.append(payload),
            cron_jobs=[
                {
                    "name": "poll-github-merges",
                    "kind": "system",
                    "handler": "poll_github_merges",
                    "interval_seconds": 60,
                }
            ],
        )

        scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)

        system_payload = next(payload for payload in dispatched if payload.get("kind") == "system")
        self.assertEqual(system_payload["handler"], "poll_github_merges")
        self.assertEqual(system_payload["lane"], "system")
        self.assertEqual(system_payload["source"], "cron")

    def test_failed_heartbeat_records_audit_without_crashing(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        audit = []

        def failing_dispatch(_payload):
            raise RuntimeError("boom")

        scheduler = Scheduler(config, failing_dispatch, audit_sink=lambda event: audit.append(event))

        scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)

        self.assertTrue(any(event["event"] == "scheduler_dispatch_failed" for event in audit))

    def test_active_hours_rejects_outside_window(self):
        job = ScheduledJob(
            agent_id="ops-agent",
            kind="heartbeat",
            interval_seconds=60,
            prompt="check",
            active_start="09:00",
            active_end="10:00",
            timezone="UTC",
        )
        utc_time = datetime.fromisoformat("2026-01-01T08:00:00+00:00")
        self.assertFalse(_within_active_hours(job, utc_time))

    def test_heartbeat_skipped_when_agent_has_no_heartbeat_config(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        for agent in config.agents:
            if agent.id == "dev-agent":
                agent.heartbeat = None
        dispatched = []
        scheduler = Scheduler(config, lambda payload: dispatched.append(payload))
        scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)
        self.assertFalse(any(payload["agent_id"] == "dev-agent" and payload["kind"] == "heartbeat" for payload in dispatched))

    def test_heartbeat_skipped_when_config_disabled(self):
        """When heartbeat config is None (disabled), no heartbeat job should be created."""
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        for agent in config.agents:
            if agent.id == "orchestrator":
                agent.heartbeat = None  # Disabled heartbeat
        dispatched = []
        scheduler = Scheduler(config, lambda payload: dispatched.append(payload))
        scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)
        self.assertFalse(any(payload["agent_id"] == "orchestrator" and payload["kind"] == "heartbeat" for payload in dispatched))

    def test_cron_dispatch_runs_in_background_thread(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        import threading

        dispatch_threads = []

        def capture_thread(payload):
            dispatch_threads.append(threading.current_thread().ident)

        scheduler = Scheduler(
            config,
            capture_thread,
            cron_jobs=[{"name": "test-cron", "agent_id": "orchestrator", "prompt": "test"}],
        )

        scheduler.run_due_once(now=datetime.fromisoformat("2026-01-01T12:00:00+00:00"), force=True)

        main_thread = threading.current_thread().ident
        self.assertTrue(len(dispatch_threads) > 0, "Cron should have dispatched")
        self.assertTrue(any(t != main_thread for t in dispatch_threads), "Cron dispatch should use background thread")

    def test_cron_expression_and_last_run_survive_restart(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched = []
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "scheduler.json"
            first = Scheduler(
                config,
                lambda payload: dispatched.append(payload),
                cron_jobs=[
                    {
                        "name": "daily-test",
                        "cron": "0 9 * * *",
                        "agent_id": "orchestrator",
                        "prompt": "run",
                        "timezone": "UTC",
                    }
                ],
                state_path=state_path,
            )

            first.run_due_once(now=datetime.fromisoformat("2026-01-01T09:00:00+00:00"))
            second = Scheduler(
                config,
                lambda payload: dispatched.append(payload),
                cron_jobs=[
                    {
                        "name": "daily-test",
                        "cron": "0 9 * * *",
                        "agent_id": "orchestrator",
                        "prompt": "run",
                        "timezone": "UTC",
                    }
                ],
                state_path=state_path,
            )
            second.run_due_once(now=datetime.fromisoformat("2026-01-01T09:01:00+00:00"))

        self.assertEqual(len([p for p in dispatched if p["kind"] == "cron"]), 1)


if __name__ == "__main__":
    unittest.main()
