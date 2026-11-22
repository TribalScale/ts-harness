"""Production alerts: each detector, de-duplication, the unconfigured skip and the system job."""

from __future__ import annotations

import importlib.util
import json
import os
import unittest
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg
from ts_harness import ops_alerts
from ts_harness.config.schema import BudgetsConfig
from ts_harness.gateway import system_jobs
from ts_harness.ops_alerts import Alert, AlertSettings, check_alerts, plan_posts

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)
Usage = namedtuple("Usage", "total used free")
QUIET_DISK = lambda _path: Usage(100, 10, 90)  # noqa: E731
HEALTHY = lambda _url: (True, "HTTP 200")  # noqa: E731


class FakeMemory:
    """The memory client calls the alert job makes, with an in-memory runtime_kv."""

    def __init__(self) -> None:
        self.stuck: list[dict] = []
        self.lost: list[dict] = []
        self.repeated: list[dict] = []
        self.spend = {3600: 0.0}
        self.day_spend = 0.0
        self.healthy = True
        self.kv: dict[tuple[str, str], tuple[object, int]] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("memory api down")

    def list_stuck_tasks(self, *, tenant_id="default"):
        self._check()
        return list(self.stuck)

    def list_coding_jobs(self, **kwargs):
        self._check()
        self.lost_query = kwargs
        return list(self.lost)

    def list_repeated_step_runs(self, *, tenant_id="default", steps=None, min_runs=5):
        self._check()
        self.repeated_query = {"steps": steps, "min_runs": min_runs}
        return [row for row in self.repeated if row["runs"] >= min_runs]

    def get_scale_metrics(self, *, window_seconds=120):
        self._check()
        cost = self.spend.get(window_seconds, self.day_spend)
        return {"llm": [{"team": "web", "window_cost_usd": cost * 0.75}, {"team": "none", "window_cost_usd": cost * 0.25}]}

    def health(self):
        return self.healthy and not self.fail

    def kv_get(self, namespace, key):
        self._check()
        value, version = self.kv.get((namespace, key), (None, 0))
        return {"value": value, "version": version}

    def kv_put(self, namespace, key, value, *, expected_version=None):
        self._check()
        _, version = self.kv.get((namespace, key), (None, 0))
        self.kv[(namespace, key)] = (json.loads(json.dumps(value)), version + 1)
        return version + 1


def _settings(**overrides) -> AlertSettings:
    return AlertSettings(**{"disk_percent": 85, **overrides})


class SettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        self.assertEqual(AlertSettings.from_env({}), AlertSettings())
        parsed = AlertSettings.from_env(
            {"TS_HARNESS_ALERT_STUCK_HOURS": "2", "TS_HARNESS_ALERT_HOURLY_USD": "0", "TS_HARNESS_ALERT_RESOLVED": "false",
             "TS_HARNESS_ALERT_LOST_JOBS": "nope"}
        )
        self.assertEqual((parsed.stuck_hours, parsed.hourly_usd, parsed.post_resolved, parsed.lost_jobs), (2.0, 0.0, False, 2))


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()

    def test_stuck_step_past_the_threshold(self):
        self.memory.stuck = [
            {"id": "t-1", "workflow_id": "w-1", "step_name": "finalize_code_review", "updated_at": (NOW - timedelta(hours=1)).isoformat(),
             "input": {"work_order_id": "PROJ-7", "_awaiting_human_decision": {"reason": "revision_limit",
                                                                              "since": (NOW - timedelta(hours=5)).isoformat()}}},
            {"id": "t-2", "workflow_id": "w-2", "step_name": "deliver_work_order", "updated_at": (NOW - timedelta(hours=1)).isoformat(),
             "input": {"work_order_id": "PAY-2"}, "error": "opencode exit -9"},
            {"id": "t-3", "workflow_id": "w-3", "workflow_type": "deliver_work_order", "step_name": "deliver_work_order",
             "updated_at": (NOW - timedelta(hours=9)).isoformat(), "input": {}, "error": "/pr_url: no PR"},
        ]
        alerts = ops_alerts.detect_stuck_steps(self.memory, _settings(), NOW)
        self.assertEqual([a.key for a in alerts], ["stuck:t-1", "stuck:t-3"])
        self.assertEqual(
            alerts[0].text,
            "PROJ-7 has waited 5 h for a decision at `finalize_code_review` (the automatic revisions ran out).",
        )
        self.assertIn("deliver_work_order w-3 has waited 9 h", alerts[1].text)
        self.assertIn("(/pr_url: no PR)", alerts[1].text)

    def test_lost_jobs_need_two_in_the_hour(self):
        self.memory.lost = [{"id": "job-1", "work_order_id": "PROJ-7"}]
        self.assertEqual(ops_alerts.detect_lost_jobs(self.memory, _settings(), NOW), [])
        self.assertEqual(self.memory.lost_query["finished_within"], 3600)
        self.memory.lost.append({"id": "abcdef123456", "work_order_id": None})
        [alert] = ops_alerts.detect_lost_jobs(self.memory, _settings(), NOW)
        self.assertEqual(alert.key, "lost_jobs:last_hour")
        self.assertIn("2 coding jobs were lost in the last hour (PROJ-7, abcdef12)", alert.text)

    def test_non_converging_work_orders(self):
        self.memory.repeated = [{"workflow_id": "w-9", "runs": 6, "work_order_id": "PROJ-9"},
                                {"workflow_id": "w-8", "runs": 3, "work_order_id": "PROJ-8"}]
        [alert] = ops_alerts.detect_non_converging(self.memory, _settings(), NOW)
        self.assertEqual(self.memory.repeated_query, {"steps": ["deliver_work_order", "implement"], "min_runs": 5})
        self.assertEqual(alert.key, "revisions:w-9")
        self.assertEqual(alert.text, "PROJ-9 is not converging: its coding step has run 6 times and the work order is still open.")

    def test_spend_last_hour_and_today(self):
        self.memory.spend = {3600: 25.0}
        self.memory.day_spend = 120.0
        [hour] = ops_alerts.detect_spend(self.memory, _settings(), NOW)
        self.assertEqual(hour.text, "LLM spend in the last hour is $25.00 (alert above $20.00).")
        self.memory.day_spend = 151.5
        keys = [a.key for a in ops_alerts.detect_spend(self.memory, _settings(), NOW)]
        self.assertEqual(keys, ["spend:hour", "spend:day"])
        self.assertEqual(ops_alerts.detect_spend(self.memory, _settings(hourly_usd=0, daily_usd=0), NOW), [])

    def test_disk_above_the_threshold(self):
        self.assertEqual(ops_alerts.detect_disk(_settings(), disk_usage=QUIET_DISK), [])
        [alert] = ops_alerts.detect_disk(_settings(), disk_usage=lambda _p: Usage(40e9, 36e9, 4e9))
        self.assertEqual(alert.key, "disk:/")
        self.assertIn("Disk / is 90% full (4.0 GB free; alert above 85%)", alert.text)

    def test_service_health(self):
        self.assertEqual(ops_alerts.detect_service_health(self.memory, http_ok=HEALTHY), [])
        self.memory.healthy = False
        with mock.patch.dict(os.environ, {"LLM_PROXY_URL": "http://llm-proxy:18800/"}):
            calls = []
            alerts = ops_alerts.detect_service_health(
                self.memory, http_ok=lambda url: (calls.append(url), (False, "connection refused"))[1]
            )
        self.assertEqual(calls, ["http://llm-proxy:18800/health"])
        self.assertEqual([a.key for a in alerts], ["health:memory_api", "health:llm_proxy"])
        self.assertIn("(connection refused)", alerts[1].text)

    def test_a_failing_detector_is_reported_not_raised(self):
        self.memory.fail = True
        alerts, failed = ops_alerts.run_detectors(self.memory, _settings(), NOW, disk_usage=QUIET_DISK, http_ok=HEALTHY)
        self.assertEqual(failed, {"stuck", "lost_jobs", "revisions", "spend"})
        self.assertEqual([a.key for a in alerts], ["health:memory_api"])


class DedupTests(unittest.TestCase):
    def test_new_repeat_and_resolved(self):
        settings = _settings(repeat_hours=6)
        alert = Alert("disk:/", "Disk / is 90% full.", "Disk / is back under 85% full.")
        state, lines = plan_posts({}, [alert], set(), NOW, settings)
        self.assertEqual(lines, ["Disk / is 90% full."])
        state, lines = plan_posts(state, [alert], set(), NOW + timedelta(hours=5), settings)
        self.assertEqual(lines, [])
        state, lines = plan_posts(state, [alert], set(), NOW + timedelta(hours=6), settings)
        self.assertEqual(lines, ["Still firing: Disk / is 90% full."])
        self.assertEqual(state["active"]["disk:/"]["first_seen"], NOW.isoformat())
        state, lines = plan_posts(state, [], set(), NOW + timedelta(hours=7), settings)
        self.assertEqual((lines, state["active"]), (["Resolved: Disk / is back under 85% full."], {}))

    def test_failed_detector_keeps_its_alerts_and_resolved_can_be_off(self):
        stuck = Alert("stuck:t-1", "PROJ-7 has waited 5 h.", "PROJ-7 is no longer waiting.")
        state, _ = plan_posts({}, [stuck], set(), NOW, _settings())
        kept, lines = plan_posts(state, [], {"stuck"}, NOW + timedelta(minutes=5), _settings())
        self.assertEqual((lines, list(kept["active"])), ([], ["stuck:t-1"]))
        _, lines = plan_posts(state, [], set(), NOW + timedelta(minutes=5), _settings(post_resolved=False))
        self.assertEqual(lines, [])


class CheckAlertsJobTests(unittest.TestCase):
    def setUp(self):
        ops_alerts._local_state.clear()
        self.memory = FakeMemory()
        self.memory.lost = [{"id": "j1", "work_order_id": "PROJ-1"}, {"id": "j2", "work_order_id": "PROJ-2"}]
        self.notifier = mock.Mock()
        self.notifier.post_thread_text.return_value = {"ok": True}
        self.config = SimpleNamespace(budgets=BudgetsConfig(notify_channel="C-OPS"))
        self.env = mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_OPS_CHANNEL": "", "SLACK_OPS_WEBHOOK_URL": "",
                                                "LLM_PROXY_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_job(self, now=NOW, **kwargs):
        return check_alerts(memory=self.memory, config=self.config, notifier=self.notifier, settings=_settings(), now=now,
                            disk_usage=QUIET_DISK, http_ok=HEALTHY, **kwargs)

    def test_skipped_without_a_channel_or_webhook(self):
        self.config = SimpleNamespace(budgets=BudgetsConfig())
        result = self.run_job()
        self.assertEqual(result["status"], "skipped")
        self.notifier.post_thread_text.assert_not_called()
        with mock.patch.dict(os.environ, {"SLACK_OPS_CHANNEL": "C-OPS", "SLACK_BOT_TOKEN": ""}):
            self.assertEqual(self.run_job()["status"], "skipped")

    def test_posts_once_then_stays_quiet_and_resolves(self):
        first = self.run_job()
        self.assertEqual((first["status"], first["firing"], first["posted"]), ("ok", ["lost_jobs:last_hour"], 1))
        kwargs = self.notifier.post_thread_text.call_args.kwargs
        self.assertEqual(kwargs["channel"], "C-OPS")
        self.assertIn("2 coding jobs were lost in the last hour", kwargs["text"])
        self.assertIn(("ops-alerts", "state"), self.memory.kv)

        self.assertEqual(self.run_job(now=NOW + timedelta(minutes=5))["posted"], 0)
        self.assertEqual(self.notifier.post_thread_text.call_count, 1)

        self.memory.lost = []
        resolved = self.run_job(now=NOW + timedelta(minutes=10))
        self.assertEqual(resolved["lines"], ["Resolved: Coding jobs are no longer being lost (fewer than 2 in the last hour)."])

    def test_a_failed_post_is_retried_next_run(self):
        self.notifier.post_thread_text.return_value = {"ok": False, "error": "channel_not_found"}
        self.assertEqual(self.run_job()["posted"], 0)
        self.notifier.post_thread_text.return_value = {"ok": True}
        self.assertEqual(self.run_job(now=NOW + timedelta(minutes=5))["posted"], 1)

    def test_dedup_survives_the_memory_api_being_down(self):
        self.run_job()
        self.memory.fail = True
        down = self.run_job(now=NOW + timedelta(minutes=5))
        self.assertEqual(down["firing"], ["health:memory_api"])
        self.assertEqual(down["lines"], ["The memory API is not answering its health check; workflows, Inbox and coding jobs are affected."])
        again = self.run_job(now=NOW + timedelta(minutes=10))
        self.assertEqual(again["lines"], [])

    def test_webhook_is_used_without_a_bot_channel(self):
        self.config = SimpleNamespace(budgets=BudgetsConfig())
        with mock.patch.dict(os.environ, {"SLACK_OPS_WEBHOOK_URL": "https://hooks.example.test/x"}), mock.patch.object(
            ops_alerts, "_post_webhook", return_value={"ok": True}
        ) as post:
            result = self.run_job()
        self.assertEqual(result["posted"], 1)
        url, text = post.call_args.args
        self.assertEqual(url, "https://hooks.example.test/x")
        self.assertIn("coding jobs were lost", text)
        self.notifier.post_thread_text.assert_not_called()


class SystemJobTests(unittest.TestCase):
    def test_dispatch_runs_the_alert_check(self):
        with mock.patch("ts_harness.ops_alerts.check_alerts", return_value={"status": "skipped"}) as run:
            self.assertEqual(system_jobs.dispatch_system_job({"handler": "check_alerts"}), {"status": "skipped"})
        run.assert_called_once_with()

    def test_dispatch_reports_errors(self):
        with mock.patch("ts_harness.ops_alerts.check_alerts", side_effect=RuntimeError("boom")):
            self.assertEqual(system_jobs.dispatch_system_job({"handler": "check_alerts"}), {"status": "error", "error": "boom"})

    def test_scheduled_every_five_minutes(self):
        cron = json.loads((REPO_ROOT / "config" / "runtime-cron-jobs.json").read_text())
        [job] = [j for j in cron["jobs"] if j["name"] == "check-alerts"]
        self.assertEqual((job["kind"], job["handler"], job["interval_seconds"]), ("system", "check_alerts", 300))
        self.assertIn('"handler": "check_alerts"', (REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh").read_text())


class DeployNotificationContractTests(unittest.TestCase):
    def test_redeploy_posts_failures_and_success_through_the_webhook_script(self):
        script = (REPO_ROOT / "scripts" / "ops" / "redeploy.sh").read_text()
        self.assertIn("trap 'notify_redeploy_failure $?' EXIT", script)
        self.assertIn('notify_redeploy_failure "$status"', script)
        self.assertIn('bash "$OPS_DIR/notify-ops.sh" "redeploy done ${deploy_sha}', script)
        notify = (REPO_ROOT / "scripts" / "ops" / "notify-ops.sh").read_text()
        self.assertIn("SLACK_OPS_WEBHOOK_URL", notify)
        self.assertIn('[[ -n "$url" ]] || exit 0', notify)

    def test_notify_is_a_no_op_without_a_webhook(self):
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "SECRETS_ENV": "/nonexistent/secrets.env"}
        result = subprocess.run(["bash", str(REPO_ROOT / "scripts" / "ops" / "notify-ops.sh"), "hello"],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_alert_settings_reach_the_runtime_env(self):
        common = (REPO_ROOT / "scripts" / "phases" / "lib" / "common.sh").read_text()
        writer = common[common.index("write_runtime_env_file() {"):common.index("enterprise_assets_commit()")]
        keys = common[common.index("INSTANCE_ENV_KEYS=("):common.index("instance_env_path()")]
        for name in ("SLACK_OPS_CHANNEL", "SLACK_OPS_WEBHOOK_URL", "TS_HARNESS_ALERT_REPEAT_HOURS", "TS_HARNESS_ALERT_DAILY_USD"):
            self.assertIn(f"        {name} \\\n", writer)
        self.assertIn("    TS_HARNESS_ALERT_STUCK_HOURS\n", keys)
        phase10 = (REPO_ROOT / "scripts" / "phases" / "10-fetch-secrets.sh").read_text()
        self.assertIn('fetch_optional_secret "slack-ops-webhook-url" 2>/dev/null', phase10)
        self.assertIn('write_env_var SLACK_OPS_WEBHOOK_URL "$SLACK_OPS_WEBHOOK_URL"', phase10)


def _gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_ops_alerts", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepeatedStepRunsRouteTests(unittest.TestCase):
    def test_query_counts_non_cancelled_runs_of_running_workflows(self):
        from tests.test_workflow_contract import FakeConnection, FakeCursor

        cursor = FakeCursor(rows=[{"workflow_id": "w-1", "workflow_type": "deliver_work_order", "runs": 6,
                                   "work_order_id": "PROJ-7", "jira_issue_key": None, "last_run_at": None}])
        result = _gateway().list_repeated_step_runs(FakeConnection(cursor), {"tenant_id": "acme", "min_runs": 5})
        self.assertEqual(result["workflows"][0]["runs"], 6)
        sql, params = cursor.calls[0]
        flat = " ".join(sql.split())
        self.assertIn("w.status = 'running'", flat)
        self.assertIn("t.status <> 'cancelled'", flat)
        self.assertIn("HAVING count(*) >= %s", flat)
        self.assertEqual(params, ("acme", ["deliver_work_order", "implement"], 5))
        with self.assertRaises(ValueError):
            _gateway().list_repeated_step_runs(FakeConnection(FakeCursor()), {"min_runs": "many"})


@requires_pg
class RepeatedStepRunsPgTests(PgTestCase):
    def test_counts_coding_runs_per_running_workflow(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            ids = {}
            for name, status in (("busy", "running"), ("calm", "running"), ("done", "succeeded")):
                cur.execute("INSERT INTO workflows (workflow_type, status) VALUES ('deliver_work_order', %s) RETURNING id", (status,))
                ids[name] = str(cur.fetchone()["id"])
            for name, runs in (("busy", 5), ("calm", 2), ("done", 7)):
                for _ in range(runs):
                    cur.execute(
                        "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, input) "
                        "VALUES (%s, 'deliver_work_order', 'dev-agent', 'succeeded', '{\"work_order_id\": \"PROJ-7\"}')",
                        (ids[name],),
                    )
            cur.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status) VALUES (%s, 'deliver_work_order', 'dev-agent', 'cancelled')",
                (ids["calm"],),
            )
            conn.commit()
        with self.db.connect() as conn:
            rows = _gateway().list_repeated_step_runs(conn, {"tenant_id": "default", "min_runs": 5})["workflows"]
        self.assertEqual([(r["workflow_id"], r["runs"], r["work_order_id"]) for r in rows], [(ids["busy"], 5, "PROJ-7")])


if __name__ == "__main__":
    unittest.main()
