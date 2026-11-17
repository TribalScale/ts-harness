"""Scale-out metrics: memory API snapshot, GitHub installation quotas, Prometheus gauges and CloudWatch payload."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import github_app as github_routes, scale_metrics  # noqa: E402

from ts_harness.integrations.github_app import GitHubApp  # noqa: E402
from ts_harness.observability import prometheus, scale_metrics as runtime_metrics  # noqa: E402

PAYLOAD_SCRIPT = REPO_ROOT / "scripts" / "ops" / "scale_metrics_payload.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


payload_script = _load(PAYLOAD_SCRIPT, "scale_metrics_payload")


@requires_pg
class SnapshotTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in ("workflow_decisions", "coding_jobs", "workflow_tasks", "workflows", "llm_usage",
                          "github_installations", "teams"):
                conn.execute(f"DELETE FROM {table}")
            self.web = str(conn.execute("INSERT INTO teams (slug, display_name) VALUES ('web', 'Web') RETURNING id").fetchone()["id"])
            self.wf = conn.execute("INSERT INTO workflows (workflow_type) VALUES ('t') RETURNING id").fetchone()["id"]
            conn.commit()

    def task(self, step="s", *, lane="agent", status="pending", age="0", deps=None, team=None, held=None, **extra):
        columns = {"workflow_id": self.wf, "step_name": step, "agent_id": "a", "lane": lane, "status": status,
                   "depends_on": deps, "team_id": team, "held_reason": held, **extra}
        names = ", ".join(columns)
        marks = ", ".join(["%s"] * len(columns))
        with self.db.connect() as conn:
            row = conn.execute(
                f"INSERT INTO workflow_tasks ({names}, created_at) VALUES ({marks}, now() - %s::interval) RETURNING id",
                (*columns.values(), f"{age} seconds"),
            ).fetchone()
            conn.commit()
        return str(row["id"])

    def snap(self, **params):
        with self.db.connect() as conn:
            return scale_metrics.snapshot(conn, params)

    def test_lanes_count_ready_held_and_running_with_oldest_age(self):
        done = self.task(status="succeeded", completed_at=None)
        self.task(age="600")
        self.task(age="30", deps=[done])
        self.task(lane="fast", age="5")
        self.task(age="900", team=self.web, held="team budget reached")
        self.task(age="999", deps=[self.task(status="running")])
        self.task(age="999", approval_required=True, approval_status="pending")
        self.task(lane="coding", status="running")
        lanes = {lane["lane"]: lane for lane in self.snap()["lanes"]}
        self.assertEqual((lanes["agent"]["ready"], lanes["agent"]["held"], lanes["agent"]["running"]), (2, 1, 1))
        self.assertAlmostEqual(lanes["agent"]["oldest_seconds"], 600, delta=5)
        self.assertEqual((lanes["fast"]["ready"], lanes["coding"]["running"], lanes["coding"]["ready"]), (1, 1, 0))

    def test_coding_jobs_queue_teams_outcomes_and_lost_rate(self):
        task = self.task(lane="coding", status="running", team=self.web)
        rows = [
            ("queued", "1200 seconds", None, None, None, "data"),
            ("queued", "60 seconds", None, None, None, None),
            ("running", "100 seconds", "90 seconds", None, task, None),
            ("starting", "5 seconds", None, None, None, "data"),
            ("succeeded", "400 seconds", "300 seconds", "60 seconds", None, None),
            ("failed", "400 seconds", "200 seconds", "20 seconds", None, None),
            ("lost", "900 seconds", "800 seconds", "10 seconds", None, None),
            ("succeeded", "9000 seconds", "8000 seconds", "7000 seconds", None, None),
        ]
        with self.db.connect() as conn:
            for status, created, started, finished, task_id, team in rows:
                conn.execute(
                    """
                    INSERT INTO coding_jobs (repo_url, spec, status, task_id, team_slug, created_at, started_at, finished_at)
                    VALUES ('https://github.com/a/b', '{}', %s, %s, %s, now() - %s::interval,
                            now() - %s::interval, now() - %s::interval)
                    """,
                    (status, task_id, team, created, started, finished),
                )
            conn.commit()
        jobs = self.snap(window_seconds=120)["coding_jobs"]
        self.assertEqual((jobs["queued"], jobs["running"]), (2, 2))
        self.assertAlmostEqual(jobs["oldest_queued_seconds"], 1200, delta=5)
        self.assertEqual(jobs["running_by_team"], [{"team": "data", "running": 1}, {"team": "web", "running": 1}])
        self.assertEqual(jobs["finished"], {"succeeded": 1, "failed": 1, "lost": 1, "cancelled": 0})
        self.assertEqual(jobs["lost_rate"], round(1 / 3, 4))
        self.assertAlmostEqual(jobs["avg_duration_seconds"], 210, delta=2)
        self.assertAlmostEqual(jobs["max_duration_seconds"], 240, delta=2)

    def test_gates_report_current_and_decided_waits(self):
        done = self.task(status="succeeded")
        with self.db.connect() as conn:
            conn.execute("UPDATE workflow_tasks SET completed_at = now() - interval '300 seconds' WHERE id = %s", (done,))
            conn.commit()
        self.task("approve_prd", age="3600", deps=[done], approval_required=True, approval_status="pending")
        self.task("approve_prd", age="60", approval_required=True, approval_status="pending")
        self.task("approve_prd", age="60", deps=[self.task(status="running")], approval_required=True, approval_status="pending")
        decided = self.task("approve_pr", age="500", status="succeeded", approval_required=True, approval_status="approved")
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO workflow_decisions (tenant_id, task_id, kind, decision, actor_id, created_at) "
                "VALUES ('default', %s, 'gate', 'approve', 'me', now() - interval '20 seconds')",
                (decided,),
            )
            conn.commit()
        gates = {g["gate"]: g for g in self.snap()["gates"]}
        self.assertEqual(gates["approve_prd"]["waiting"], 2)
        self.assertAlmostEqual(gates["approve_prd"]["oldest_seconds"], 300, delta=5)
        self.assertEqual((gates["approve_pr"]["waiting"], gates["approve_pr"]["decided"]), (0, 1))
        self.assertAlmostEqual(gates["approve_pr"]["avg_wait_seconds"], 480, delta=5)

    def test_github_rate_limits_are_recorded_per_installation(self):
        with self.db.connect() as conn:
            for installation, login in ((1, "acme"), (2, "beta")):
                github_routes.upsert_installation(conn, {"host": "github.com", "installation_id": installation, "account_login": login})
            result = github_routes.record_rate_limits(conn, {"limits": [
                {"host": "github.com", "installation_id": 1, "remaining": 400, "limit": 5000, "reset": 1900000000},
                {"host": "github.com", "installation_id": 9, "remaining": 1, "limit": 5000},
            ]})
            self.assertEqual(result, {"updated": 1})
            with self.assertRaises(ValueError):
                github_routes.record_rate_limits(conn, {"limits": [{"host": "github.com", "installation_id": 1}]})
            listed = github_routes.list_installations(conn, {})["installations"]
        self.assertEqual(listed[0]["rate_limit"]["remaining"], 400)
        self.assertIsNone(listed[1]["rate_limit"]["remaining"])
        [github] = self.snap()["github"]
        self.assertEqual((github["installation_id"], github["remaining_percent"], github["account"]), (1, 8.0, "acme"))

    def test_llm_tokens_and_cost_by_team(self):
        with self.db.connect() as conn:
            for team, tokens, cost, age in ((self.web, 100, 1.5, "10 seconds"), (self.web, 50, 0.5, "1 hour"), (None, 7, 0.1, "5 seconds")):
                conn.execute(
                    "INSERT INTO llm_usage (team_id, provider, model, input_tokens, output_tokens, cost_usd, ts) "
                    "VALUES (%s, 'openai', 'gpt-4o', %s, %s, %s, GREATEST(now() - %s::interval, date_trunc('month', now())))",
                    (team, tokens, tokens * 2, cost, age),
                )
            conn.commit()
        llm = {row["team"]: row for row in self.snap(window_seconds=60)["llm"]}
        self.assertEqual((llm["web"]["window_input_tokens"], llm["web"]["window_output_tokens"]), (100, 200))
        self.assertEqual((llm["web"]["window_cost_usd"], llm["none"]["window_cost_usd"]), (1.5, 0.1))
        self.assertGreaterEqual(llm["web"]["month_cost_usd"], 1.5)

    def test_window_is_validated(self):
        for bad in ("abc", 5, 100000):
            with self.assertRaises(ValueError):
                self.snap(window_seconds=bad)


SNAPSHOT = {
    "window_seconds": 120,
    "lanes": [{"lane": "agent", "ready": 3, "held": 1, "running": 2, "oldest_seconds": 42.0}],
    "coding_jobs": {
        "queued": 2, "oldest_queued_seconds": 950.0, "running": 1,
        "running_by_team": [{"team": "web", "running": 1}],
        "finished": {"succeeded": 3, "failed": 0, "lost": 1, "cancelled": 0},
        "lost_rate": 0.25, "avg_duration_seconds": 300.0, "max_duration_seconds": 400.0,
    },
    "gates": [{"gate": "approve_prd", "waiting": 1, "oldest_seconds": 600.0, "decided": 0, "avg_wait_seconds": 0.0}],
    "github": [
        {"host": "github.com", "installation_id": 1, "account": "acme", "remaining": 400, "limit": 5000, "remaining_percent": 8.0, "age_seconds": 10},
        {"host": "github.com", "installation_id": 2, "account": "beta", "remaining": 4000, "limit": 5000, "remaining_percent": 80.0, "age_seconds": 10},
    ],
    "llm": [{"team": "web", "window_input_tokens": 10, "window_output_tokens": 20, "window_cost_usd": 0.3, "month_cost_usd": 12.0}],
}


class CloudWatchPayloadTests(unittest.TestCase):
    def test_snapshot_maps_to_dimensioned_metrics(self):
        metrics = {(m["name"], tuple(sorted((m.get("dimensions") or {}).items()))): m for m in payload_script.cloudwatch_metrics(SNAPSHOT)}
        self.assertEqual(metrics[("LaneReadyTasks", (("Lane", "agent"),))]["value"], 3)
        self.assertEqual(metrics[("CodingJobOldestQueuedSeconds", ())]["unit"], "Seconds")
        self.assertEqual(metrics[("CodingJobsFinishedTotal", ())]["value"], 4)
        self.assertEqual(metrics[("CodingJobsRunning", (("Team", "web"),))]["value"], 1)
        self.assertEqual(metrics[("GitHubRateLimitLowestPercent", ())]["value"], 8.0)
        self.assertIn(("GitHubRateLimitRemaining", (("Installation", "github.com/1"),)), metrics)
        self.assertEqual(metrics[("LlmMonthCostUsd", (("Team", "web"),))]["value"], 12.0)
        self.assertNotIn(("GateAvgWaitSeconds", (("Gate", "approve_prd"),)), metrics, "no decided gates in the window")

    def test_script_writes_an_adapter_payload_and_tolerates_a_bad_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot, out = Path(tmp, "s.json"), Path(tmp, "o.json")
            snapshot.write_text(json.dumps(SNAPSHOT))
            subprocess.run([sys.executable, str(PAYLOAD_SCRIPT), str(snapshot), str(out), "i-123"], check=True)
            payload = json.loads(out.read_text())
            self.assertEqual((payload["namespace"], payload["dimensions"], payload["instance_id"]), ("TSHarness", {}, "i-123"))
            self.assertTrue(payload["metrics"])
            snapshot.write_text("not json")
            subprocess.run([sys.executable, str(PAYLOAD_SCRIPT), str(snapshot), str(out)], check=True)
            self.assertEqual(json.loads(out.read_text())["metrics"], [])


@unittest.skipUnless(prometheus.PROMETHEUS_AVAILABLE, "prometheus_client not installed")
class PrometheusGaugeTests(unittest.TestCase):
    def test_snapshot_sets_gauges_and_drops_departed_labels(self):
        runtime_metrics.observe_snapshot(SNAPSHOT)
        text = prometheus.generate_latest()
        self.assertIn('ts_harness_lane_ready_tasks{lane="agent"} 3.0', text)
        self.assertIn('ts_harness_coding_jobs_running{team="web"} 1.0', text)
        self.assertIn('ts_harness_github_installation_rate_limit_remaining{host="github.com",installation="1"} 400.0', text)
        self.assertIn('ts_harness_llm_tokens{direction="output",team="web"} 20.0', text)
        runtime_metrics.observe_snapshot({**SNAPSHOT, "coding_jobs": {**SNAPSHOT["coding_jobs"], "running_by_team": []}})
        self.assertNotIn('ts_harness_coding_jobs_running{team="web"}', prometheus.generate_latest())

    def test_refresh_caches_for_thirty_seconds_and_survives_memory_errors(self):
        runtime_metrics._cache.update(at=0.0, snapshot=None)
        memory = mock.Mock()
        memory.get_scale_metrics.return_value = SNAPSHOT
        now = [100.0]
        runtime_metrics.refresh_prometheus(memory, clock=lambda: now[0])
        runtime_metrics.refresh_prometheus(memory, clock=lambda: now[0])
        self.assertEqual(memory.get_scale_metrics.call_count, 1)
        now[0] = 131.0
        memory.get_scale_metrics.side_effect = RuntimeError("down")
        runtime_metrics.refresh_prometheus(memory, clock=lambda: now[0])
        self.assertEqual(memory.get_scale_metrics.call_count, 2)


class GitHubRateLimitJobTests(unittest.TestCase):
    def test_reads_each_active_installation_and_stores_the_limits(self):
        memory = mock.Mock()
        memory.list_github_installations.return_value = [
            {"host": "github.com", "installation_id": 1},
            {"host": "github.com", "installation_id": 2},
            {"host": "github.com", "installation_id": 3, "suspended_at": "2026-01-01"},
            {"host": "ghe.acme.dev", "installation_id": 4},
        ]
        app = mock.Mock()
        app.installation_rate_limit.side_effect = [{"remaining": 10, "limit": 5000, "reset": 1}, RuntimeError("boom")]
        result = runtime_metrics.refresh_github_rate_limits(memory, app_lookup=lambda host: app if host == "github.com" else None)
        memory.record_github_rate_limits.assert_called_once_with([{"host": "github.com", "installation_id": 1, "remaining": 10, "limit": 5000, "reset": 1}])
        self.assertEqual((result["status"], result["installations"]), ("partial", 1))
        self.assertIn("github.com/2", result["errors"][0])

    def test_memory_failure_is_reported(self):
        memory = mock.Mock()
        memory.list_github_installations.side_effect = RuntimeError("down")
        self.assertEqual(runtime_metrics.refresh_github_rate_limits(memory)["status"], "error")

    def test_handler_is_registered(self):
        from ts_harness.gateway.system_jobs import SYSTEM_HANDLERS

        self.assertIn("github_rate_limits", SYSTEM_HANDLERS)

    def test_app_reuses_the_installation_token_and_reads_core_limits(self):
        calls = []

        def transport(method, url, headers, data):
            calls.append((method, url, headers["Authorization"]))
            if url.endswith("/access_tokens"):
                return 201, json.dumps({"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}).encode(), {}
            return 200, json.dumps({"resources": {"core": {"remaining": 4321, "limit": 5000, "reset": 99}}}).encode(), {}

        app = GitHubApp("1", "key", transport=transport)
        with mock.patch.object(GitHubApp, "jwt", return_value="jwt"):
            first = app.installation_rate_limit(7)
            app.installation_rate_limit(7)
        self.assertEqual(first, {"remaining": 4321, "limit": 5000, "reset": 99})
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in calls], ["access_tokens", "rate_limit", "rate_limit"])
        self.assertEqual(calls[1][2], "Bearer ghs_x")


class RunnerJobDurationTests(unittest.TestCase):
    def test_duration_prefers_started_then_claimed(self):
        runner = _load(REPO_ROOT / "scripts" / "workflow-runner.py", "workflow_runner_scale_metrics")
        job = {"created_at": "2026-10-22T10:00:00Z", "claimed_at": "2026-10-22T10:01:00Z", "finished_at": "2026-10-22T10:06:00+00:00"}
        self.assertEqual(runner._job_duration_seconds(job), 300.0)
        self.assertEqual(runner._job_duration_seconds({**job, "started_at": "2026-10-22T10:05:00Z"}), 60.0)
        self.assertIsNone(runner._job_duration_seconds({"created_at": "2026-10-22T10:00:00Z"}))


class ScaleOutMetricsContractTests(unittest.TestCase):
    def test_publish_script_sends_the_snapshot_without_instance_dimension(self):
        script = (REPO_ROOT / "scripts" / "ops" / "publish-runtime-metrics.sh").read_text()
        self.assertIn("/metrics/scale-out?window_seconds=120", script)
        self.assertIn("scale_metrics_payload.py", script)
        self.assertIn('cloud_publish_runtime_metrics "$SCALE_JSON"', script)
        self.assertIn("Authorization: Bearer", script)

    def test_adapters_accept_per_metric_dimensions(self):
        aws = (REPO_ROOT / "scripts" / "cloud" / "aws" / "adapter.sh").read_text()
        gcp = (REPO_ROOT / "scripts" / "cloud" / "gcp" / "adapter.sh").read_text()
        self.assertIn('{**dims, **(metric.get("dimensions") or {})}', aws)
        self.assertIn('metric.get("dimensions")', gcp)
        self.assertIn('payload.get("instance_id")', gcp)

    def test_monitoring_has_dashboard_and_scale_out_alarms(self):
        tf = (REPO_ROOT / "infra" / "aws" / "monitoring.tf").read_text()
        for needle in ('resource "aws_cloudwatch_dashboard" "scale_out"', "CodingJobOldestQueuedSeconds",
                       "IF(finished >= 3, lost / finished, 0)", "GitHubRateLimitLowestPercent"):
            self.assertIn(needle, tf)
        published = {m["name"] for m in payload_script.cloudwatch_metrics(SNAPSHOT)}
        for name in ("CodingJobOldestQueuedSeconds", "CodingJobsLost", "CodingJobsFinishedTotal", "GitHubRateLimitLowestPercent"):
            self.assertIn(name, published)

    def test_cron_job_is_scheduled_in_both_places(self):
        cron = json.loads((REPO_ROOT / "config" / "runtime-cron-jobs.json").read_text())
        self.assertIn("github_rate_limits", {j["handler"] for j in cron["jobs"]})
        self.assertIn('"handler": "github_rate_limits"', (REPO_ROOT / "scripts" / "phases" / "15-automation-cron.sh").read_text())


if __name__ == "__main__":
    unittest.main()
