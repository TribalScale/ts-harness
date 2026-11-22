"""Program LLM budgets: spend attribution, the effective budget, warn once, downgrade and stop, the override and access."""

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

from memory_routes import budgets, coding_jobs  # noqa: E402

from ts_harness.budgets import program_alert_text, program_budget_alerts, program_downgrade_note  # noqa: E402
from ts_harness.config.schema import BudgetsConfig, parse_budgets  # noqa: E402
from ts_harness.gateway.budgets_bff import (  # noqa: E402
    BudgetAccessDenied,
    program_budget_admin,
    set_program_budget_admin,
)

SPEC = {"spec_version": 1, "repo_url": "https://github.com/acme/app", "base_branch": "main", "work_order_id": "PROJ-1"}


class _Base(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            for table in ("coding_jobs", "workflow_tasks", "workflows", "llm_usage", "program_budget_alerts",
                          "budget_settings", "projects", "delivery_programs", "teams"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def call(self, fn, *args):
        with self.db.connect() as conn:
            return fn(conn, *args)

    def program(self, name: str = "Checkout", *, source: str | None = "slack:C-PROG:111.222") -> str:
        with self.db.connect() as conn:
            row = conn.execute(
                "INSERT INTO delivery_programs (summary, display_name, entry_phase, current_phase, source_channel) "
                "VALUES ('p', %s, 'prd', 'prd', %s) RETURNING id",
                (name, source),
            ).fetchone()
            conn.commit()
        return str(row["id"])

    def workflow(self, program: str | None = None, parent: str | None = None) -> str:
        with self.db.connect() as conn:
            row = conn.execute(
                "INSERT INTO workflows (workflow_type, program_id, parent_workflow_id) VALUES ('deliver_work_order', %s, %s) RETURNING id",
                (program, parent),
            ).fetchone()
            conn.commit()
        return str(row["id"])

    def spend(self, program: str, usd: float) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO llm_usage (program_id, provider, model, cost_usd) VALUES (%s, 'anthropic', 'claude', %s)",
                (program, usd),
            )
            conn.commit()

    def settings(self, **extra):
        self.call(budgets.put_budget_settings, {"warn_at_percent": 80, **extra})


@requires_pg
class SpendAndBudgetTests(_Base):
    def test_coding_usage_is_attributed_to_the_program_of_its_workflow(self):
        program = self.program()
        child = self.workflow(parent=self.workflow(program=program))
        row = {"tenant_id": "default", "workflow_id": child, "task_id": None, "program_id": None, "spec": {}}
        result = {"usage": [{"provider": "anthropic", "model": "claude", "input_tokens": 10, "output_tokens": 5, "cost_usd": 2.5}]}
        with self.db.connect() as conn, conn.cursor() as cur:
            self.assertEqual(coding_jobs._record_usage(cur, row, result), 1)
            conn.commit()
        self.assertEqual(self.call(budgets.program_budget, program)["spent_usd"], 2.5)

    def test_default_then_override_then_back_to_default(self):
        program = self.program()
        status = self.call(budgets.program_budget, program)
        self.assertEqual((status["source"], status["status"], status["percent"]), ("none", "none", None))
        self.settings(program_default_usd=50, program_action="stop")
        self.spend(program, 10)
        status = self.call(budgets.program_budget, program)
        self.assertEqual((status["source"], status["budget_usd"], status["percent"], status["action"]), ("default", 50.0, 20.0, "stop"))
        status = self.call(budgets.set_program_budget, program, {"amount_usd": 12, "reason": "small run", "actor": "a@x"})
        self.assertEqual((status["source"], status["budget_usd"], status["status"], status["set_by"]), ("program", 12.0, "warning", "a@x"))
        status = self.call(budgets.set_program_budget, program, {"amount_usd": None, "reason": "default", "actor": "a@x"})
        self.assertEqual((status["source"], status["budget_usd"]), ("default", 50.0))
        with self.db.connect() as conn:
            audits = conn.execute("SELECT count(*) AS n FROM session_audit_log WHERE action_type = 'program_budget_set'").fetchone()
        self.assertEqual(audits["n"], 2)

    def test_set_validates(self):
        program = self.program()
        with self.assertRaises(ValueError):
            self.call(budgets.set_program_budget, program, {"amount_usd": 5})
        with self.assertRaises(ValueError):
            self.call(budgets.set_program_budget, program, {"amount_usd": -1, "reason": "r"})
        with self.assertRaises(LookupError):
            self.call(budgets.set_program_budget, "nope", {"amount_usd": 1, "reason": "r"})
        with self.assertRaises(LookupError):
            self.call(budgets.set_program_budget, program, {"amount_usd": 1, "reason": "r", "tenant_id": "other"})

    def test_program_endpoint_carries_the_program_budget(self):
        program = self.program()
        self.settings(program_default_usd=20)
        self.spend(program, 5)
        result = self.call(budgets.program_team_budget, program, {})
        self.assertIsNone(result["team"])
        self.assertEqual((result["program"]["spent_usd"], result["program"]["budget_usd"]), (5.0, 20.0))


@requires_pg
class ProgramAlertTests(_Base):
    def test_warning_and_reached_once_and_reset_by_a_raise(self):
        program = self.program()
        self.settings(program_default_usd=100)
        self.assertEqual(self.call(budgets.claim_program_budget_alerts, {})["alerts"], [])
        self.spend(program, 85)
        [warning] = self.call(budgets.claim_program_budget_alerts, {})["alerts"]
        self.assertEqual((warning["level"], warning["percent"], warning["source_channel"]), ("warning", 85.0, "slack:C-PROG:111.222"))
        self.assertEqual(self.call(budgets.claim_program_budget_alerts, {})["alerts"], [])
        self.spend(program, 20)
        [reached] = self.call(budgets.claim_program_budget_alerts, {})["alerts"]
        self.assertEqual((reached["level"], reached["action"]), ("reached", "downgrade"))
        self.assertEqual(self.call(budgets.claim_program_budget_alerts, {})["alerts"], [])
        self.call(budgets.set_program_budget, program, {"amount_usd": 120, "reason": "more", "actor": "a"})
        self.assertEqual(self.call(budgets.claim_program_budget_alerts, {})["alerts"], [])
        self.spend(program, 20)
        self.assertEqual([a["level"] for a in self.call(budgets.claim_program_budget_alerts, {})["alerts"]], ["reached"])

    def test_programs_without_a_budget_are_never_alerted(self):
        self.spend(self.program(), 1000)
        self.assertEqual(self.call(budgets.claim_program_budget_alerts, {})["alerts"], [])


@requires_pg
class StopAndDowngradeTests(_Base):
    def job(self, program: str, *, link_program: bool = True) -> str:
        workflow = self.workflow(program=program)
        with self.db.connect() as conn:
            return coding_jobs.create_coding_job(
                conn, {"spec": SPEC, "workflow_id": workflow, "program_id": program if link_program else None}
            )["id"]

    def held(self, job: str):
        with self.db.connect() as conn:
            return conn.execute("SELECT held_reason, budget_note FROM coding_jobs WHERE id = %s", (job,)).fetchone()

    def test_stop_holds_new_jobs_with_a_ticket_note_until_raised(self):
        program = self.program()
        self.settings(program_default_usd=10, program_action="stop")
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret"}):
            job = self.job(program, link_program=False)
            self.spend(program, 10)
            first = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
            self.assertEqual((first["jobs"], first["held_by_program_budget"]), ([], 1))
            row = self.held(job)
            self.assertEqual(row["held_reason"], "program budget reached")
            self.assertIn("on hold: program Checkout reached its LLM budget (10.00 of 10.00 USD)", row["budget_note"])
            self.assertEqual(self.call(budgets.program_budget, program)["held_jobs"], 1)
            [note] = self.call(budgets.claim_program_budget_alerts, {})["notes"]
            self.assertEqual((note["id"], note["work_order_id"]), (job, "PROJ-1"))
            self.assertEqual(self.call(budgets.claim_program_budget_alerts, {})["notes"], [])
            self.call(budgets.set_program_budget, program, {"amount_usd": 50, "reason": "more", "actor": "a"})
            second = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
        self.assertEqual([j["id"] for j in second["jobs"]], [job])
        self.assertIsNone(self.held(job)["held_reason"])

    def test_downgrade_never_holds(self):
        program = self.program()
        self.settings(program_default_usd=10)
        with mock.patch.dict(os.environ, {"CODING_JOB_SECRET": "s3cret"}):
            job = self.job(program)
            self.spend(program, 20)
            claimed = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
        self.assertEqual(([j["id"] for j in claimed["jobs"]], claimed["held_by_program_budget"]), ([job], 0))

    def test_a_downgraded_job_keeps_its_note_for_the_ticket(self):
        program = self.program()
        with self.db.connect() as conn:
            job = coding_jobs.create_coding_job(conn, {"spec": {**SPEC, "budget_note": "default model"}, "program_id": program})
        self.assertEqual(job["budget_note"], "default model")
        self.assertEqual([n["budget_note"] for n in self.call(budgets.claim_program_budget_alerts, {})["notes"]], ["default model"])


class _Memory:
    def __init__(self, *, team=True, tenant="default"):
        self.team, self.tenant = team, tenant
        self.set: list[tuple[str, dict]] = []

    def get_program_budget(self, program_id):
        program = {"id": program_id, "budget_usd": 100.0, "source": "default", "spent_usd": 85.0, "spent_month_usd": 20.0,
                   "percent": 85.0, "status": "warning", "action": "stop", "currency": "USD", "warn_at_percent": 80,
                   "held_jobs": 0, "set_by": None, "set_at": None, "source_channel": "slack:C:1"}
        team = {"id": "t-web", "slug": "web", "status": "ok", "percent": 3.0, "held_tasks": 0} if self.team else None
        return {"tenant_id": self.tenant, "team": team, "program": program}

    def set_program_budget(self, program_id, **body):
        self.set.append((program_id, body))
        return {}


def _auth(*, admin=False, teams=(), tenant="default"):
    return SimpleNamespace(is_service=admin, teams=[{"slug": s, "role": r} for s, r in teams], tenant_id=tenant, actor_id="me@x")


class ProgramBudgetAccessTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(budgets=BudgetsConfig())
        patcher = mock.patch("ts_harness.policy.permissions.actor_permissions", return_value=set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_for_everyone_with_who_can_set_it(self):
        memory = _Memory()
        admin = program_budget_admin(memory, _auth(admin=True), self.config, "p-1")["program"]
        self.assertEqual((admin["can_set"], admin["set_limit"], admin["spent_usd"]), (True, None, 85.0))
        self.assertNotIn("source_channel", admin)
        manager = program_budget_admin(memory, _auth(teams=[("web", "manager")]), self.config, "p-1")["program"]
        self.assertEqual((manager["can_set"], manager["set_limit"]), (True, 10000.0))
        member = program_budget_admin(memory, _auth(teams=[("web", "member")]), self.config, "p-1")["program"]
        self.assertFalse(member["can_set"])
        self.assertEqual(program_budget_admin(memory, _auth(tenant="other"), self.config, "p-1"), {"team": None, "program": None})

    def test_admin_sets_any_amount_and_can_clear_it(self):
        memory = _Memory(team=False)
        set_program_budget_admin(memory, _auth(admin=True), self.config, "p-1", {"amount_usd": 50000, "reason": "r"})
        set_program_budget_admin(memory, _auth(admin=True), self.config, "p-1", {"amount_usd": None, "reason": "r"})
        self.assertEqual([body["amount_usd"] for _, body in memory.set], [50000.0, None])
        self.assertEqual(memory.set[0][1]["actor"], "me@x")

    def test_managers_of_the_team_up_to_the_limit_only(self):
        memory = _Memory()
        manager = _auth(teams=[("web", "manager")])
        set_program_budget_admin(memory, manager, self.config, "p-1", {"amount_usd": 10000, "reason": "r"})
        with self.assertRaises(BudgetAccessDenied):
            set_program_budget_admin(memory, manager, self.config, "p-1", {"amount_usd": 10001, "reason": "r"})
        with self.assertRaises(BudgetAccessDenied):
            set_program_budget_admin(memory, _auth(teams=[("web", "member")]), self.config, "p-1", {"amount_usd": 5, "reason": "r"})
        with self.assertRaises(BudgetAccessDenied):
            set_program_budget_admin(_Memory(team=False), manager, self.config, "p-1", {"amount_usd": 5, "reason": "r"})
        with self.assertRaises(LookupError):
            set_program_budget_admin(_Memory(tenant="other"), manager, self.config, "p-1", {"amount_usd": 5, "reason": "r"})
        with self.assertRaises(ValueError):
            set_program_budget_admin(memory, manager, self.config, "p-1", {"amount_usd": "lots", "reason": "r"})
        self.assertEqual(len(memory.set), 1)


class ProgramBudgetConfigTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual((parse_budgets(None).program_default_usd, parse_budgets(None).program_action), (0.0, "downgrade"))
        parsed = parse_budgets({"program_default_usd": 150, "program_action": "STOP"})
        self.assertEqual((parsed.program_default_usd, parsed.program_action), (150.0, "stop"))
        self.assertEqual(parsed.settings_payload()["program_action"], "stop")
        for bad in ({"program_action": "pause"}, {"program_default_usd": -1}):
            with self.assertRaises(ValueError):
                parse_budgets(bad)


class ProgramAlertJobTests(unittest.TestCase):
    def test_alert_text_says_which_action(self):
        base = {"name": "Checkout", "spent_usd": 101, "budget_usd": 100, "currency": "USD", "percent": 101.0}
        stop = program_alert_text({**base, "level": "reached", "action": "stop"})
        self.assertIn("Program *Checkout* reached its LLM budget (101.00 of 100.00 USD). Action taken: *stop*", stop)
        self.assertIn("new coding jobs for its tickets wait", stop)
        downgrade = program_alert_text({**base, "level": "reached", "action": "downgrade"})
        self.assertIn("use the default coding model instead of the stronger one", downgrade)
        warning = program_alert_text({**base, "level": "warning", "percent": 82.0, "spent_usd": 82, "action": "stop"})
        self.assertEqual(warning, "Program *Checkout* has used 82.0% of its LLM budget (82.00 of 100.00 USD). "
                                  "At 100%, new coding jobs for its tickets wait.")

    def test_posts_to_the_program_thread_ops_channel_and_ticket_threads(self):
        memory = mock.Mock()
        memory.claim_program_budget_alerts.return_value = {
            "alerts": [
                {"id": "p-1", "name": "Checkout", "level": "reached", "action": "stop", "spent_usd": 10, "budget_usd": 10,
                 "source_channel": "slack:C-PROG:111.222"},
                {"id": "p-2", "name": "Web chat", "level": "warning", "action": "stop", "spent_usd": 8, "budget_usd": 10,
                 "percent": 80.0, "source_channel": "web:web:abc"},
            ],
            "notes": [{"id": "j-1", "workflow_id": "wf-child", "work_order_id": "PROJ-1", "budget_note": "on hold"}],
        }
        workflows = {
            "wf-child": {"workflow": {"source_channel": "", "parent_workflow_id": "wf-parent"}},
            "wf-parent": {"workflow": {"source_channel": "slack:C-PROG:111.222"}},
        }
        memory.get_workflow.side_effect = lambda wid: workflows[wid]
        notifier = mock.Mock()
        notifier.post_thread_text.return_value = {"ok": True}
        result = program_budget_alerts(memory, notifier, channel="C-OPS")
        calls = [c.kwargs for c in notifier.post_thread_text.call_args_list]
        targets = [(c["channel"], c.get("thread_ts")) for c in calls]
        self.assertEqual(targets, [("C-PROG", "111.222"), ("C-OPS", None), ("C-OPS", None), ("C-PROG", "111.222")])
        self.assertEqual(calls[-1]["text"], "PROJ-1: on hold")
        self.assertEqual(result["posted"], 4)
        self.assertEqual(result["alerts"][0], {"program": "p-1", "level": "reached", "action": "stop"})

    def test_memory_errors_are_reported(self):
        memory = mock.Mock()
        memory.claim_program_budget_alerts.side_effect = RuntimeError("down")
        self.assertEqual(program_budget_alerts(memory, mock.Mock())["status"], "error")

    def test_downgrade_note_only_at_the_budget_with_downgrade(self):
        memory = mock.Mock()
        program = {"name": "Checkout", "status": "reached", "action": "downgrade", "spent_usd": 12, "budget_usd": 10, "currency": "USD"}
        memory.get_program_budget.return_value = {"program": program}
        note = program_downgrade_note("p-1", memory=memory)
        self.assertIn("Program Checkout reached its LLM budget (12.00 of 10.00 USD)", note)
        self.assertIn("default coding model", note)
        memory.get_program_budget.return_value = {"program": {**program, "action": "stop"}}
        self.assertIsNone(program_downgrade_note("p-1", memory=memory))
        memory.get_program_budget.return_value = {"program": {**program, "status": "warning"}}
        self.assertIsNone(program_downgrade_note("p-1", memory=memory))
        memory.get_program_budget.side_effect = RuntimeError("down")
        self.assertIsNone(program_downgrade_note("p-1", memory=memory))
        self.assertIsNone(program_downgrade_note(None, memory=memory))


if __name__ == "__main__":
    unittest.main()
