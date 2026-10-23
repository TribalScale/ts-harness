"""Team LLM budgets: effective budget, holds at 100%, raises, alerts, proxy admission and access."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import budgets, coding_jobs, task_claims  # noqa: E402

from ts_harness.budgets import alert_text, budget_alerts, proxy_admission  # noqa: E402
from ts_harness.config.schema import BudgetsConfig, parse_budgets  # noqa: E402
from ts_harness.gateway.budgets_bff import (  # noqa: E402
    BudgetAccessDenied,
    list_budgets_admin,
    program_budget_admin,
    raise_budget_admin,
)

SPEC = {"spec_version": 1, "repo_url": "https://github.com/acme/app", "base_branch": "main", "work_order_id": "WO-1"}


class _Base(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in ("coding_jobs", "workflow_tasks", "workflows", "llm_usage", "team_budget_overrides",
                          "team_budget_alerts", "budget_settings", "projects", "delivery_programs", "teams"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def team(self, slug: str) -> str:
        with self.db.connect() as conn:
            row = conn.execute("INSERT INTO teams (slug, display_name) VALUES (%s, %s) RETURNING id", (slug, slug.title())).fetchone()
            conn.commit()
        return str(row["id"])

    def spend(self, team: str | None, usd: float, *, months_ago: int = 0) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO llm_usage (team_id, provider, model, cost_usd, ts) "
                "VALUES (%s, 'openai', 'gpt-4o', %s, now() - make_interval(months => %s))",
                (team, usd, months_ago),
            )
            conn.commit()

    def budget(self, team: str) -> float:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT team_monthly_budget('default', %s, date_trunc('month', now())::date) AS b", (team,)
            ).fetchone()
        return float(row["b"])

    def call(self, fn, *args):
        with self.db.connect() as conn:
            return fn(conn, *args)


@requires_pg
class EffectiveBudgetTests(_Base):
    def test_month_raise_then_permanent_then_team_config_then_default(self):
        web = self.team("web")
        self.assertEqual(self.budget(web), 5000)
        self.call(budgets.put_budget_settings, {"default_team_monthly": 6000, "teams": {}})
        self.assertEqual(self.budget(web), 6000)
        self.call(budgets.put_budget_settings, {"default_team_monthly": 6000, "teams": {"web": 7000}})
        self.assertEqual(self.budget(web), 7000)
        self.call(budgets.raise_team_budget, web, {"amount_usd": 8000, "scope": "permanent", "reason": "roadmap", "actor": "a"})
        self.assertEqual(self.budget(web), 8000)
        self.call(budgets.raise_team_budget, "web", {"amount_usd": 9000, "scope": "month", "reason": "launch", "actor": "a"})
        self.assertEqual(self.budget(web), 9000)
        self.call(budgets.raise_team_budget, web, {"amount_usd": 8500, "scope": "month", "reason": "trim", "actor": "a"})
        self.assertEqual(self.budget(web), 8500)

    def test_team_config_by_id_and_only_this_months_spend_counts(self):
        web = self.team("web")
        self.call(budgets.put_budget_settings, {"teams": {web: 100}})
        self.spend(web, 60)
        self.spend(web, 500, months_ago=1)
        self.spend(None, 3)
        summary = self.call(budgets.list_team_budgets, {})
        [row] = summary["teams"]
        self.assertEqual((row["budget_usd"], row["spent_usd"], row["percent"], row["status"]), (100.0, 60.0, 60.0, "ok"))
        self.assertEqual(summary["unassigned_spent_usd"], 3.0)
        self.spend(web, 25)
        self.assertEqual(self.call(budgets.team_budget, "web", {})["status"], "warning")
        self.spend(web, 15)
        self.assertEqual(self.call(budgets.team_budget, web, {})["status"], "reached")

    def test_raise_validates_and_audits(self):
        web = self.team("web")
        bad = [
            ({"amount_usd": -1, "reason": "x"}, ValueError),
            ({"amount_usd": "lots", "reason": "x"}, ValueError),
            ({"amount_usd": 10, "reason": " "}, ValueError),
            ({"amount_usd": 10, "reason": "x", "scope": "forever"}, ValueError),
        ]
        for body, error in bad:
            with self.assertRaises(error):
                self.call(budgets.raise_team_budget, web, body)
        with self.assertRaises(LookupError):
            self.call(budgets.raise_team_budget, "nope", {"amount_usd": 10, "reason": "x"})
        result = self.call(budgets.raise_team_budget, web, {"amount_usd": 7000, "scope": "month", "reason": "launch", "actor": "lead@x"})
        self.assertEqual((result["budget_usd"], result["overrides"][0]["raised_by"]), (7000.0, "lead@x"))
        with self.db.connect() as conn:
            audit = conn.execute("SELECT * FROM session_audit_log WHERE action_type = 'team_budget_raised' AND agent_id = 'lead@x' ORDER BY occurred_at DESC LIMIT 1").fetchone()
        self.assertEqual((audit["agent_id"], audit["details"]["team_slug"], audit["details"]["scope"]), ("lead@x", "web", "month"))


@requires_pg
class HoldAtBudgetTests(_Base):
    def task(self, team: str | None, lane: str = "agent") -> str:
        with self.db.connect() as conn:
            wf = conn.execute("INSERT INTO workflows (workflow_type) VALUES ('t') RETURNING id").fetchone()["id"]
            row = conn.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, lane, team_id) VALUES (%s, 's', 'dev-agent', %s, %s) RETURNING id",
                (wf, lane, team),
            ).fetchone()
            conn.commit()
        return str(row["id"])

    def claim(self, lane: str = "agent") -> list[str]:
        with self.db.connect() as conn:
            return [str(t["id"]) for t in task_claims.claim_batch(conn, {"runner": "r", "lane": lane, "limit": 10, "lease_seconds": 60})["tasks"]]

    def held_reason(self, task: str):
        with self.db.connect() as conn:
            return conn.execute("SELECT held_reason FROM workflow_tasks WHERE id = %s", (task,)).fetchone()["held_reason"]

    def test_over_budget_team_waits_with_a_reason_until_raised(self):
        web, data = self.team("web"), self.team("data")
        self.call(budgets.put_budget_settings, {"default_team_monthly": 100})
        self.spend(web, 100)
        held = self.task(web)
        free, unowned, fast = self.task(data), self.task(None), self.task(web, lane="fast")
        self.assertEqual(set(self.claim()), {free, unowned})
        self.assertEqual(self.held_reason(held), "team budget reached")
        self.assertEqual(self.claim("fast"), [fast])
        self.assertEqual(self.call(budgets.team_budget, "web", {})["held_tasks"], 1)

        self.call(budgets.raise_team_budget, web, {"amount_usd": 200, "reason": "more", "actor": "a"})
        self.assertEqual(self.claim(), [held])
        self.assertIsNone(self.held_reason(held))

    def test_coding_jobs_of_over_budget_teams_stay_queued(self):
        web = self.team("web")
        self.call(budgets.put_budget_settings, {"default_team_monthly": 10})
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret"}):
            with self.db.connect() as conn:
                wf = conn.execute("INSERT INTO workflows (workflow_type) VALUES ('deliver_work_order') RETURNING id").fetchone()["id"]
                conn.commit()
            task = self.task(web, lane="coding")
            with self.db.connect() as conn:
                conn.execute("UPDATE workflow_tasks SET workflow_id = %s WHERE id = %s", (wf, task))
                conn.commit()
                job = coding_jobs.create_coding_job(conn, {"spec": SPEC, "workflow_id": str(wf), "task_id": task})["id"]
            self.spend(web, 10)
            first = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
            self.assertEqual((first["jobs"], first["held_by_budget"]), ([], 1))
            self.call(budgets.raise_team_budget, web, {"amount_usd": 50, "reason": "more", "actor": "a"})
            second = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
        self.assertEqual([j["id"] for j in second["jobs"]], [job])


@requires_pg
class AlertTests(_Base):
    def test_warning_and_reached_are_sent_once_and_reset_by_a_raise(self):
        web = self.team("web")
        self.call(budgets.put_budget_settings, {"default_team_monthly": 100, "warn_at_percent": 80})
        self.assertEqual(self.call(budgets.claim_budget_alerts, {})["alerts"], [])
        self.spend(web, 85)
        [warning] = self.call(budgets.claim_budget_alerts, {})["alerts"]
        self.assertEqual((warning["slug"], warning["level"], warning["percent"]), ("web", "warning", 85.0))
        self.assertEqual(self.call(budgets.claim_budget_alerts, {})["alerts"], [])
        self.spend(web, 20)
        [reached] = self.call(budgets.claim_budget_alerts, {})["alerts"]
        self.assertEqual(reached["level"], "reached")
        self.call(budgets.raise_team_budget, web, {"amount_usd": 120, "reason": "more", "actor": "a"})
        self.assertEqual(self.call(budgets.claim_budget_alerts, {})["alerts"], [])
        self.spend(web, 20)
        self.assertEqual([a["level"] for a in self.call(budgets.claim_budget_alerts, {})["alerts"]], ["reached"])

    def test_new_team_at_full_budget_gets_one_reached_alert(self):
        web = self.team("web")
        self.call(budgets.put_budget_settings, {"default_team_monthly": 10})
        self.spend(web, 12)
        alerts = self.call(budgets.claim_budget_alerts, {})["alerts"]
        self.assertEqual([a["level"] for a in alerts], ["reached"])


@requires_pg
class ProgramAndAdmissionTests(_Base):
    def test_program_budget_follows_the_project_team(self):
        web = self.team("web")
        with self.db.connect() as conn:
            conn.execute("INSERT INTO projects (key, name, team_id) VALUES ('WEB', 'Web', %s)", (web,))
            with_team = conn.execute(
                "INSERT INTO delivery_programs (summary, entry_phase, current_phase, project_id) VALUES ('p', 'prd', 'prd', 'WEB') RETURNING id"
            ).fetchone()["id"]
            without = conn.execute(
                "INSERT INTO delivery_programs (summary, entry_phase, current_phase) VALUES ('p', 'prd', 'prd') RETURNING id"
            ).fetchone()["id"]
            conn.commit()
        result = self.call(budgets.program_team_budget, str(with_team), {})
        self.assertEqual((result["tenant_id"], result["team"]["slug"]), ("default", "web"))
        self.assertIsNone(self.call(budgets.program_team_budget, str(without), {})["team"])
        with self.assertRaises(LookupError):
            self.call(budgets.program_team_budget, "not-a-uuid", {})

    def test_proxy_refuses_teams_at_budget_by_token_team_or_task(self):
        web = self.team("web")
        self.call(budgets.put_budget_settings, {"default_team_monthly": 10})
        with self.db.connect() as conn:
            wf = conn.execute("INSERT INTO workflows (workflow_type) VALUES ('t') RETURNING id").fetchone()["id"]
            task = conn.execute(
                "INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, team_id) VALUES (%s, 's', 'a', %s) RETURNING id",
                (wf, web),
            ).fetchone()["id"]
            conn.commit()
        now = [0.0]
        admit = proxy_admission(self.db.connect, cache_seconds=30, clock=lambda: now[0])
        self.assertIsNone(admit({"team": web}))
        self.assertIsNone(admit({"user": "someone"}))
        self.spend(web, 10)
        self.assertIsNone(admit({"team": web}), "cached for 30 seconds")
        now[0] = 31
        self.assertIn("reached its monthly LLM budget", admit({"team": web}))
        self.assertIn(web, admit({"task": str(task)}))
        self.assertIsNone(admit({"task": "not-a-uuid"}))

    def test_proxy_admits_when_the_database_fails(self):
        def broken():
            raise RuntimeError("down")

        self.assertIsNone(proxy_admission(broken)({"team": "t"}))


class _Memory:
    def __init__(self):
        self.raised: list[tuple[str, dict]] = []

    def list_team_budgets(self, *, tenant_id):
        return {
            "month": "2026-10-01",
            "currency": "USD",
            "unassigned_spent_usd": 4.0,
            "teams": [
                {"id": "t-web", "slug": "web", "status": "reached", "percent": 101.0, "held_tasks": 2, "budget_usd": 5000},
                {"id": "t-data", "slug": "data", "status": "ok", "percent": 10.0, "held_tasks": 0, "budget_usd": 5000},
            ],
        }

    def raise_team_budget(self, team_id, **body):
        self.raised.append((team_id, body))
        return {"id": team_id}

    def get_program_budget(self, program_id):
        return {"tenant_id": "default", "team": {"id": "t-web", "slug": "web", "status": "reached", "percent": 101.0,
                                                 "held_tasks": 2, "budget_usd": 5000, "spent_usd": 5050}}


def _auth(*, admin=False, teams=(), tenant="default"):
    return SimpleNamespace(is_service=admin, teams=[{"slug": s, "role": r} for s, r in teams], tenant_id=tenant, actor_id="me@x")


class BudgetAccessTests(unittest.TestCase):
    def setUp(self):
        self.memory = _Memory()
        self.config = SimpleNamespace(budgets=BudgetsConfig())
        patcher = mock.patch("ts_harness.policy.permissions.actor_permissions", return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_admin_sees_everything_and_raises_without_a_limit(self):
        data = list_budgets_admin(self.memory, _auth(admin=True), self.config)
        self.assertEqual(([t["slug"] for t in data["teams"]], data["raise_limit"], data["unassigned_spent_usd"]), (["web", "data"], None, 4.0))
        raise_budget_admin(self.memory, _auth(admin=True), self.config, "web", {"amount_usd": 50000, "reason": "r"})
        self.assertEqual(self.memory.raised[0][0], "t-web")
        self.assertEqual(self.memory.raised[0][1]["amount_usd"], 50000.0)

    def test_manager_sees_own_teams_and_raises_up_to_the_ceiling(self):
        auth = _auth(teams=[("web", "manager"), ("data", "member")])
        data = list_budgets_admin(self.memory, auth, self.config)
        self.assertEqual({t["slug"]: t["can_raise"] for t in data["teams"]}, {"web": True, "data": False})
        self.assertEqual(data["raise_limit"], 10000.0)
        self.assertNotIn("unassigned_spent_usd", data)
        raise_budget_admin(self.memory, auth, self.config, "t-web", {"amount_usd": 10000, "scope": "permanent", "reason": "r"})
        self.assertEqual(self.memory.raised[0][1]["scope"], "permanent")
        with self.assertRaises(BudgetAccessDenied):
            raise_budget_admin(self.memory, auth, self.config, "web", {"amount_usd": 10001, "reason": "r"})
        with self.assertRaises(BudgetAccessDenied):
            raise_budget_admin(self.memory, auth, self.config, "data", {"amount_usd": 10, "reason": "r"})
        with self.assertRaises(LookupError):
            raise_budget_admin(self.memory, auth, self.config, "nope", {"amount_usd": 10, "reason": "r"})

    def test_outsiders_see_no_teams(self):
        self.assertEqual(list_budgets_admin(self.memory, _auth(), self.config)["teams"], [])

    def test_program_banner_shows_status_only_and_respects_the_tenant(self):
        team = program_budget_admin(self.memory, _auth(), self.config, "p-1")["team"]
        self.assertEqual(team, {"id": "t-web", "slug": "web", "display_name": None, "status": "reached", "percent": 101.0, "held_tasks": 2})
        self.assertIsNone(program_budget_admin(self.memory, _auth(tenant="other"), self.config, "p-1")["team"])


class BudgetConfigAndAlertJobTests(unittest.TestCase):
    def test_parse_defaults_and_validation(self):
        self.assertEqual(parse_budgets(None), BudgetsConfig())
        parsed = parse_budgets({"currency": "eur", "teams": {"web": 7000}, "warn_at_percent": 90})
        self.assertEqual((parsed.currency, parsed.teams, parsed.warn_at_percent), ("EUR", {"web": 7000.0}, 90))
        for bad in ({"default_team_monthly": -1}, {"warn_at_percent": 0}, {"teams": ["web"]}, {"teams": {"web": True}}):
            with self.assertRaises(ValueError):
                parse_budgets(bad)

    def test_job_syncs_settings_and_posts_each_alert(self):
        memory = mock.Mock()
        memory.claim_budget_alerts.return_value = {
            "alerts": [{"slug": "web", "display_name": "Web", "level": "reached", "spent_usd": 5010.0, "budget_usd": 5000.0, "percent": 100.2}]
        }
        notifier = mock.Mock()
        notifier.post_thread_text.return_value = {"ok": True}
        config = SimpleNamespace(budgets=BudgetsConfig(notify_channel="C-OPS", teams={"web": 5000}))
        result = budget_alerts(memory=memory, config=config, notifier=notifier)
        memory.put_budget_settings.assert_called_once_with(tenant_id="default", **config.budgets.settings_payload())
        self.assertEqual(result, {"status": "ok", "alerts": [{"team": "web", "level": "reached"}], "sent": 1})
        kwargs = notifier.post_thread_text.call_args.kwargs
        self.assertEqual(kwargs["channel"], "C-OPS")
        self.assertIn("*Web* reached its monthly LLM budget (5,010.00 of 5,000.00 USD)", kwargs["text"])

    def test_job_reports_memory_errors(self):
        memory = mock.Mock()
        memory.put_budget_settings.side_effect = RuntimeError("memory down")
        result = budget_alerts(memory=memory, config=SimpleNamespace(budgets=BudgetsConfig()), notifier=mock.Mock())
        self.assertEqual(result["status"], "error")

    def test_warning_text(self):
        text = alert_text({"display_name": "Data", "level": "warning", "percent": 82.5, "spent_usd": 4125, "budget_usd": 5000}, currency="USD")
        self.assertEqual(text, "*Data* has used 82.5% of its monthly LLM budget (4,125.00 of 5,000.00 USD).")


if __name__ == "__main__":
    unittest.main()
