"""Stale scheduled-job detection, its metric/alarm wiring, and the RDS restore drill."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.health import build_health_report
from ts_harness.gateway.scheduler import Scheduler

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
DRILL = REPO_ROOT / "scripts" / "ops" / "restore-drill-smoke.sh"


def _scheduler(jobs, *, started_at: float) -> Scheduler:
    config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
    scheduler = Scheduler(config, lambda _payload: None, cron_jobs=jobs)
    scheduler.started_at = started_at
    return scheduler


POLL = {"name": "poll-github-merges", "kind": "system", "handler": "poll_github_merges", "interval_seconds": 60}
DAILY = {"name": "daily-standup", "cron": "0 9 * * *", "agent_id": "comms-agent", "prompt": "standup"}


class StaleJobTests(unittest.TestCase):
    def test_interval_job_is_stale_after_two_missed_intervals(self):
        scheduler = _scheduler([POLL], started_at=NOW.timestamp() - 3600)
        scheduler._state["poll-github-merges"] = {"last_run": NOW.timestamp() - 121}

        stale = scheduler.stale_jobs(NOW)

        self.assertEqual([job["name"] for job in stale], ["poll-github-merges"])
        self.assertEqual(stale[0]["interval_seconds"], 60)
        self.assertEqual(stale[0]["overdue_seconds"], 1)

    def test_interval_job_within_two_intervals_is_healthy(self):
        scheduler = _scheduler([POLL], started_at=NOW.timestamp() - 3600)
        scheduler._state["poll-github-merges"] = {"last_run": NOW.timestamp() - 119}

        self.assertEqual(scheduler.stale_jobs(NOW), [])

    def test_cron_job_uses_gap_between_fire_times(self):
        scheduler = _scheduler([DAILY], started_at=NOW.timestamp() - 10 * 86400)
        scheduler._state["daily-standup"] = {"last_run": NOW.timestamp() - 1.5 * 86400}
        self.assertEqual(scheduler.stale_jobs(NOW), [])

        scheduler._state["daily-standup"] = {"last_run": NOW.timestamp() - 2.5 * 86400}
        stale = scheduler.stale_jobs(NOW)
        self.assertEqual([job["name"] for job in stale], ["daily-standup"])
        self.assertEqual(stale[0]["interval_seconds"], 86400)

    def test_never_run_job_gets_a_boot_grace_period(self):
        booted = _scheduler([POLL], started_at=NOW.timestamp() - 100)
        self.assertEqual(booted.stale_jobs(NOW), [])

        long_up = _scheduler([POLL], started_at=NOW.timestamp() - 500)
        stale = long_up.stale_jobs(NOW)
        self.assertEqual([job["name"] for job in stale], ["poll-github-merges"])
        self.assertIsNone(stale[0]["last_run"])

    def test_heartbeats_are_not_reported(self):
        scheduler = _scheduler([], started_at=0)

        self.assertEqual(scheduler.stale_jobs(NOW), [])

    def test_health_report_marks_stale_jobs_degraded(self):
        stale = [{"name": "poll-github-merges", "kind": "system"}]

        payload = build_health_report(stale_jobs=stale).to_dict()

        component = payload["components"]["scheduler_jobs"]
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(component["status"], "degraded")
        self.assertEqual(component["stale_count"], 1)
        self.assertIn("poll-github-merges", component["detail"])

    def test_unconfigured_integrations_do_not_degrade_overall_health(self):
        payload = build_health_report(
            provider_ok=True,
            memory_ok=True,
            mcp_ok=True,
            scheduler_ok=True,
            google_workspace_ok=False,
            google_workspace_detail=(
                "GOOGLE_WORKSPACE_SA_JSON or GOOGLE_WORKSPACE_SA_JSON_PATH "
                "must be set for delegation"
            ),
            channels_ok=False,
            channels_detail=(
                "No Slack, Discord, or Telegram token is configured. Set SLACK_BOT_TOKEN."
            ),
            sandbox_ok=False,
            sandbox_detail="Set SANDBOX_WORKER_URL or set SANDBOX_ENABLED=false.",
        ).to_dict()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["components"]["channels"]["status"], "not_configured")
        self.assertIn("SLACK_BOT_TOKEN", payload["components"]["channels"]["detail"])
        self.assertEqual(payload["components"]["sandbox"]["status"], "not_configured")
        self.assertIn("SANDBOX_WORKER_URL", payload["components"]["sandbox"]["detail"])
        self.assertEqual(payload["components"]["google_workspace"]["status"], "not_configured")
        self.assertIn("GOOGLE_WORKSPACE_SA_JSON", payload["components"]["google_workspace"]["detail"])

        failed = build_health_report(
            google_workspace_ok=False,
            google_workspace_detail="delegation probe failed",
        ).to_dict()
        self.assertEqual(failed["components"]["google_workspace"]["status"], "degraded")
        self.assertEqual(failed["status"], "degraded")

    def test_health_report_without_stale_jobs_is_ok(self):
        payload = build_health_report(stale_jobs=[]).to_dict()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["components"]["scheduler_jobs"]["stale_count"], 0)


class StaleJobAlarmContractTests(unittest.TestCase):
    def test_publisher_emits_system_jobs_stale_from_healthz(self):
        script = (REPO_ROOT / "scripts" / "ops" / "publish-runtime-metrics.sh").read_text()

        self.assertIn('"SystemJobsStale"', script)
        self.assertIn('["scheduler_jobs"]', script)
        self.assertIn('["stale_count"]', script)

    def test_cloudwatch_alarm_pages_on_stale_jobs(self):
        tf = (REPO_ROOT / "infra" / "aws" / "monitoring.tf").read_text()
        start = tf.index('resource "aws_cloudwatch_metric_alarm" "system_jobs_stale"')
        block = tf[start : tf.index("\n}\n", start)]

        self.assertIn('metric_name         = "SystemJobsStale"', block)
        self.assertIn('comparison_operator = "GreaterThanThreshold"', block)
        self.assertIn("threshold           = 0", block)
        self.assertIn("evaluation_periods  = 3", block)
        self.assertIn("period              = 120", block)
        self.assertIn('treat_missing_data  = "breaching"', block)
        self.assertIn("alarm_actions       = [aws_sns_topic.alerts.arn]", block)


AWS_STUB = """#!/usr/bin/env bash
echo "$*" >> "$AWS_LOG"
case "$*" in
  *"--db-instance-identifier drill-1 --query"*)
    printf 'drill.example\\t5432\\tts_harness\\tts_harness\\tarn:aws:secretsmanager:drill\\n' ;;
  *"describe-db-instances"*)
    printf 'subnet-a\\tsg-1,sg-2\\tdb.t3.small\\n' ;;
  *"describe-db-snapshots"*)
    echo "rds:src-db-2026-09-29" ;;
  *"wait db-instance-available"*)
    if [[ -n "${AWS_WAIT_FAILS:-}" ]]; then exit 255; fi ;;
  *"get-secret-value"*)
    echo '{"username":"ts_harness","password":"pw"}' ;;
  *)
    echo '{}' ;;
esac
"""

PSQL_STUB = """#!/usr/bin/env bash
echo "PGPASSWORD=$PGPASSWORD $*" >> "$PSQL_LOG"
case "$*" in
  *schema_migrations*) cat "$APPLIED_MIGRATIONS" ;;
  *) echo 3 ;;
esac
"""


class RestoreDrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("aws", AWS_STUB), ("psql", PSQL_STUB)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.migrations = root / "migrations"
        self.migrations.mkdir()
        for name in ("001_a.sql", "002_b.sql"):
            (self.migrations / name).write_text("-- noop\n")
        self.applied = root / "applied.txt"
        self.aws_log = root / "aws.log"
        self.psql_log = root / "psql.log"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "AWS_LOG": str(self.aws_log),
            "PSQL_LOG": str(self.psql_log),
            "APPLIED_MIGRATIONS": str(self.applied),
            "MIGRATIONS_DIR": str(self.migrations),
            "RDS_INSTANCE_ID": "src-db",
            "DRILL_INSTANCE_ID": "drill-1",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(DRILL), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _aws_calls(self) -> list[str]:
        return self.aws_log.read_text().splitlines() if self.aws_log.exists() else []

    def test_successful_drill_restores_checks_and_deletes(self):
        self.applied.write_text("001_a.sql\n002_b.sql\n")

        result = self._run("--rds")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RDS restore drill OK", result.stdout)
        self.assertIn("delivery_programs: 3 rows", result.stdout)
        calls = self._aws_calls()
        verbs = [next(word for word in call.split() if word != "rds") for call in calls]
        self.assertEqual(
            verbs,
            [
                "describe-db-instances",
                "describe-db-snapshots",
                "restore-db-instance-from-db-snapshot",
                "wait",
                "describe-db-instances",
                "secretsmanager",
                "delete-db-instance",
            ],
        )
        restore = calls[2]
        self.assertIn("--db-snapshot-identifier rds:src-db-2026-09-29", restore)
        self.assertIn("--vpc-security-group-ids sg-1 sg-2", restore)
        self.assertIn("--db-subnet-group-name subnet-a", restore)
        self.assertIn("--no-publicly-accessible", restore)
        self.assertIn("--skip-final-snapshot", calls[-1])
        self.assertIn("drill-1", calls[-1])
        self.assertIn("PGPASSWORD=pw", self.psql_log.read_text())

    def test_missing_migration_fails_but_still_deletes(self):
        self.applied.write_text("001_a.sql\n")

        result = self._run("--rds")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("002_b.sql", result.stderr)
        self.assertIn("delete-db-instance", self._aws_calls()[-1])

    def test_restore_failure_mid_drill_still_deletes(self):
        self.applied.write_text("001_a.sql\n002_b.sql\n")

        result = self._run("--rds", AWS_WAIT_FAILS="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("delete-db-instance", self._aws_calls()[-1])
        self.assertFalse(self.psql_log.exists())

    def test_dry_run_prints_calls_and_executes_nothing(self):
        result = self._run("--rds", "--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._aws_calls(), [])
        self.assertFalse(self.psql_log.exists())
        dry = [line for line in result.stderr.splitlines() if line.startswith("[dry-run] aws")]
        self.assertIn("restore-db-instance-from-db-snapshot", dry[2])
        self.assertIn("delete-db-instance", dry[-1])

    def test_rds_mode_requires_instance_id(self):
        result = self._run("--rds", RDS_INSTANCE_ID="")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self._aws_calls(), [])

    def test_default_mode_stays_non_destructive(self):
        result = self._run(TS_HARNESS_DB_HOST="127.0.0.1", TS_HARNESS_DB_PORT="1")

        self.assertEqual(self._aws_calls(), [])
        self.assertIn("non-destructive", result.stdout)


if __name__ == "__main__":
    unittest.main()
