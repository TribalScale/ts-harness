"""Delivery quality metrics over synthetic tickets: one per metric, plus the endpoint's access check."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app
from ts_harness.gateway.auth import AuthContext
from ts_harness.workflows import delivery_metrics as dm

NOW = datetime(2026, 1, 14, 12, 0, tzinfo=timezone.utc)  # a Wednesday


def at(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


BLOCKING = {"severity": "major", "category": "correctness", "file": "a.py", "line": 1, "message": "Off by one."}


def review(status: str, fixed: int = 0) -> dict:
    return {"status": status, "fixed": fixed, "reviews": 1, "open_blocking": [BLOCKING] if status == "open" else []}


def job(task_id, *, start, end, cost, model="claude-sonnet-5-5", status="succeeded", pre_push=None):
    return {"id": f"j-{task_id}-{start}", "task_id": task_id, "status": status, "model": model,
            "created_at": at(start + 1), "started_at": at(start), "finished_at": at(end),
            "result": {"ok": status == "succeeded", "usage": [{"model": model, "cost_usd": cost}],
                       **({"pre_push_review": pre_push} if pre_push else {})}}


def first_try_ticket(key="PROJ-1", *, start=300):
    """Coded once (review fixed one issue), CI and code review passed, merged after a 30-minute wait."""
    tasks = [
        {"id": "prep", "step_name": "prepare_ticket_delivery", "agent_id": "orchestrator", "status": "succeeded",
         "created_at": at(start), "started_at": at(start), "completed_at": at(start - 1), "input": {"jira_issue_key": key}},
        {"id": "dev1", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "succeeded", "depends_on": ["prep"],
         "created_at": at(start), "started_at": at(start - 1), "completed_at": at(start - 60),
         "output": {"pr_url": "https://github.com/o/r/pull/1"}},
        {"id": "ci1", "step_name": "await_pr_checks", "agent_id": "orchestrator", "status": "succeeded", "depends_on": ["dev1"],
         "created_at": at(start), "started_at": at(start - 60), "completed_at": at(start - 70)},
        {"id": "fin1", "step_name": "finalize_code_review", "agent_id": "orchestrator", "status": "succeeded", "depends_on": ["ci1"],
         "created_at": at(start), "started_at": at(start - 70), "completed_at": at(start - 80), "output": {"verdict": "pass"}},
        {"id": "merge", "step_name": "await_human_merge", "agent_id": "orchestrator", "status": "succeeded", "depends_on": ["fin1"],
         "created_at": at(start), "started_at": at(start - 80), "completed_at": at(start - 110), "output": {}},
    ]
    jobs = [job("dev1", start=start - 1, end=start - 60, cost=2.0, pre_push=review("pass", fixed=1))]
    return key, tasks, jobs


def revised_ticket(key="PROJ-2"):
    """Pre-push review passed, code review blocked anyway; a /revise ran on Opus after a lost job; then a CI revision; merged."""
    tasks = [
        {"id": "prep", "step_name": "prepare_ticket_delivery", "agent_id": "orchestrator", "status": "succeeded",
         "created_at": at(600), "started_at": at(600), "completed_at": at(599), "input": {"jira_issue_key": key}},
        {"id": "dev1", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "succeeded", "depends_on": ["prep"],
         "created_at": at(600), "started_at": at(599), "completed_at": at(560), "output": {"pr_url": "https://github.com/o/r/pull/2"}},
        {"id": "fin1", "step_name": "finalize_code_review", "agent_id": "orchestrator", "status": "cancelled", "depends_on": ["dev1"],
         "created_at": at(600), "started_at": at(560), "updated_at": at(500), "output": {"verdict": "block", "findings": [BLOCKING]}},
        {"id": "dev2", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "succeeded", "depends_on": ["dev1"],
         "created_at": at(500), "started_at": at(500), "completed_at": at(450),
         "input": {"_revision_source": "finalize_code_review", "coding_model": "anthropic/claude-opus-5-5",
                   "_revision_grant": {"actor": "ada@example.com", "at": at(500), "waiting_since": at(560)}}},
        {"id": "dev3", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "succeeded", "depends_on": ["dev2"],
         "created_at": at(440), "started_at": at(440), "completed_at": at(420), "input": {"_revision_budget": "checks"}},
        {"id": "fin2", "step_name": "finalize_code_review", "agent_id": "orchestrator", "status": "succeeded", "depends_on": ["dev3"],
         "created_at": at(440), "started_at": at(420), "completed_at": at(415), "output": {"verdict": "pass"}},
        {"id": "merge", "step_name": "await_human_merge", "agent_id": "orchestrator", "status": "succeeded", "depends_on": ["fin2"],
         "created_at": at(440), "started_at": at(415), "completed_at": at(400), "output": {}},
    ]
    jobs = [
        job("dev1", start=599, end=560, cost=1.0, pre_push=review("pass")),
        job("dev2", start=499, end=495, cost=0.5, model="claude-opus-5-5", status="lost"),
        job("dev2", start=494, end=450, cost=3.0, model="claude-opus-5-5", pre_push=review("open")),
        job("dev3", start=439, end=420, cost=1.5, model="claude-opus-5-5", pre_push=review("no_review")),
    ]
    return key, tasks, jobs


def cancelled_ticket(key="PROJ-3"):
    tasks = [
        {"id": "prep", "step_name": "prepare_ticket_delivery", "agent_id": "orchestrator", "status": "succeeded",
         "created_at": at(12000), "started_at": at(12000), "completed_at": at(11999), "input": {"jira_issue_key": key}},
        {"id": "dev1", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "cancelled", "depends_on": ["prep"],
         "created_at": at(12000), "started_at": at(11999), "updated_at": at(11990), "output": {"pr_url": "https://github.com/o/other/pull/3"}},
    ]
    return key, tasks, [job("dev1", start=11999, end=11990, cost=0.25, status="cancelled")]


def memory_for(tickets, *, statuses=None, completed=None):
    statuses = statuses or {}
    completed = completed or {}
    memory = MagicMock()
    memory.list_programs.return_value = {"programs": [
        {"id": "p1", "display_name": "Alpha", "project_id": "PROJ", "updated_at": at(5)},
        {"id": "p2", "display_name": "Hidden", "project_id": "PROJ", "updated_at": at(5)},
    ]}
    workflows = {f"wf-{key}": (tasks, jobs) for key, tasks, jobs in tickets}
    memory.get_program.side_effect = lambda pid: {"workflows": [
        {"id": wid, "workflow_type": "implement_jira_ticket", "status": statuses.get(wid, "succeeded")}
        for wid in workflows] if pid == "p1" else []}
    memory.get_workflow_children.return_value = []
    memory.get_workflow.side_effect = lambda wid: {
        "workflow": {"id": wid, "workflow_type": "implement_jira_ticket", "status": statuses.get(wid, "succeeded"),
                     "completed_at": completed.get(wid, at(100))},
        "tasks": workflows[wid][0]}

    def list_jobs(workflow_id=None, **kwargs):
        if workflow_id is None:
            return []
        return workflows[workflow_id][1]

    memory.list_coding_jobs.side_effect = list_jobs
    return memory


def offline_guard(**kwargs):
    """A guard context whose GitHub and Jira calls fail, as when they are down."""
    from ts_harness.improvement.guard import GuardContext

    def down(*args, **kw):
        raise RuntimeError("offline")

    return GuardContext(now=NOW, github_for=down, jira_factory=down, memory=MagicMock(), **kwargs)


class DeliveryMetricsTests(unittest.TestCase):
    def metrics(self, **kwargs):
        tickets = [first_try_ticket(), revised_ticket()]
        kwargs.setdefault("guard", False)
        return dm.delivery_metrics(memory_for(tickets), allowed=lambda pid: pid == "p1", now=NOW, **kwargs)

    def rows(self):
        return {r["jira_issue_key"]: r for r in self.metrics()["tickets"]}

    def test_first_try_pass(self):
        rows, summary = self.rows(), self.metrics()["summary"]
        self.assertTrue(rows["PROJ-1"]["first_try"])
        self.assertFalse(rows["PROJ-2"]["first_try"])
        self.assertEqual((summary["finished"], summary["merged"], summary["first_try"], summary["first_try_rate"]), (2, 2, 1, 0.5))

    def test_revisions_split_by_trigger(self):
        rows, summary = self.rows(), self.metrics()["summary"]
        self.assertEqual(rows["PROJ-2"]["revisions"], 2)
        self.assertEqual(rows["PROJ-2"]["revisions_by_trigger"], {"human": 1, "ci": 1})
        self.assertEqual({k: summary["revisions"][k] for k in ("median", "mean", "max", "total")},
                         {"median": 1, "mean": 1, "max": 2, "total": 2})
        self.assertEqual(summary["revisions"]["by_trigger"], {"ci": 1, "human": 1})

    def test_time_to_merge_and_waiting_for_a_person(self):
        rows, summary = self.rows(), self.metrics()["summary"]
        self.assertEqual(rows["PROJ-1"]["time_to_merge_seconds"], 109 * 60)  # first coding start to merge
        self.assertEqual(rows["PROJ-2"]["time_to_merge_seconds"], 199 * 60)
        self.assertEqual(rows["PROJ-1"]["you_seconds"], 30 * 60)  # the merge wait
        self.assertEqual(rows["PROJ-2"]["you_seconds"], 60 * 60 + 15 * 60)  # the /revise wait and the merge wait
        self.assertEqual(rows["PROJ-1"]["agent_seconds"], 59 * 60 + 10 * 60)  # coding and the passed code review
        self.assertEqual(summary["time_to_merge_seconds"]["median"], (109 + 199) * 60 / 2)
        self.assertGreater(summary["you_share"], 0)

    def test_pre_push_review_catch_rate(self):
        summary = self.metrics()["summary"]
        # Reviewed runs: PROJ-1 (fixed 1), PROJ-2 dev1 (pass), dev2 (open). no_review and the lost job do not count.
        self.assertEqual((summary["reviewed_runs"], summary["review_fixed"]), (3, 1))
        self.assertEqual(summary["review_catch_rate"], round(1 / 3, 3))

    def test_gate_disagreement(self):
        rows, summary = self.rows(), self.metrics()["summary"]
        self.assertEqual((rows["PROJ-1"]["gate_compared"], rows["PROJ-1"]["gate_disagreed"]), (1, 0))
        # PROJ-2: the first run's review passed and code review blocked; the last run had no review.
        self.assertEqual((rows["PROJ-2"]["gate_compared"], rows["PROJ-2"]["gate_disagreed"]), (1, 1))
        self.assertEqual(summary["gate_disagreement_rate"], 0.5)

    def test_cost_and_stronger_model_share(self):
        rows, summary = self.rows(), self.metrics()["summary"]
        self.assertEqual(rows["PROJ-2"]["cost_usd"], 6.0)
        self.assertEqual(rows["PROJ-2"]["strong_cost_usd"], 5.0)  # the Opus runs, the lost one included
        self.assertEqual(summary["cost_usd"], {"median": 4.0, "total": 8.0})
        self.assertEqual(summary["strong_cost_share"], round(5 / 8, 3))
        self.assertEqual([r["jira_issue_key"] for r in self.metrics()["tickets"]], ["PROJ-2", "PROJ-1"])  # costliest first

    def test_lost_jobs(self):
        self.assertEqual(self.rows()["PROJ-2"]["lost_jobs"], 1)
        self.assertEqual(self.metrics()["summary"]["lost_jobs"], 1)

    def test_period_and_filters(self):
        tickets = [first_try_ticket(), revised_ticket(), cancelled_ticket()]
        memory = memory_for(tickets, statuses={"wf-PROJ-3": "cancelled"}, completed={"wf-PROJ-3": at(11990)})
        week = dm.delivery_metrics(memory, days=7, now=NOW, guard=False)
        self.assertEqual(sorted(r["jira_issue_key"] for r in week["tickets"]), ["PROJ-1", "PROJ-2"])
        month = dm.delivery_metrics(memory, days=30, now=NOW, guard=False)
        self.assertEqual(month["summary"]["finished"], 3)
        self.assertEqual(month["facets"]["repos"], ["o/other", "o/r"])
        only = dm.delivery_metrics(memory, days=30, repo="O/Other", now=NOW, guard=False)
        self.assertEqual([r["jira_issue_key"] for r in only["tickets"]], ["PROJ-3"])
        self.assertEqual(dm.delivery_metrics(memory, days=30, project="OTHER", now=NOW, guard=False)["tickets"], [])
        weeks = [w["week"] for w in month["weekly"]]
        self.assertEqual(weeks, sorted(weeks))
        self.assertEqual(month["weekly"][-1], {"week": "2026-01-12", "tickets": 2, "first_try_rate": 0.5,
                                              "autonomy_rate": 0.5, "revisions_mean": 1.0})

    def test_running_tickets_are_optional(self):
        key, tasks, jobs = first_try_ticket("PROJ-4")
        tasks = [dict(t) for t in tasks[:2]]
        tasks[1].update({"status": "blocked", "completed_at": None, "error": "awaiting_external:coding.finished"})
        memory = memory_for([(key, tasks, [{**jobs[0], "status": "running", "finished_at": None, "result": {}}])],
                            statuses={"wf-PROJ-4": "running"})
        self.assertEqual(dm.delivery_metrics(memory, now=NOW, guard=False)["tickets"], [])
        out = dm.delivery_metrics(memory, include_running=True, now=NOW, guard=False)
        self.assertEqual(out["tickets"][0]["outcome"], "running")
        self.assertEqual((out["summary"]["tickets"], out["summary"]["finished"], out["summary"]["first_try_rate"]), (1, 0, None))


class MetricsEndpointTests(unittest.TestCase):
    def setUp(self):
        runtime = mock.Mock()
        runtime.config = SimpleNamespace(identity=SimpleNamespace(role_bundles={}, service_permissions=None))
        self.auth = AuthContext(actor_id="ada", tenant_id="default", roles=["viewer"], auth_provider="oidc")
        self.memory = memory_for([first_try_ticket(), revised_ticket()])

        def level(program_id, auth, level, **kwargs):
            if program_id != "p1":
                raise asgi_app.ProgramAccessDenied("no access", 403)

        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=runtime),
            mock.patch.object(asgi_app, "_auth", side_effect=lambda request, body: self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            mock.patch.object(asgi_app, "_require_program_level", side_effect=level),
            # The fixtures are dated around NOW, not the wall clock.
            mock.patch.object(dm, "delivery_metrics", side_effect=lambda *a, _real=dm.delivery_metrics, **k: _real(
                *a, now=NOW, guard_context=offline_guard(), **k)),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_only_readable_programs_count(self):
        self.memory.get_program.side_effect = lambda pid: {"workflows": [
            {"id": "wf-PROJ-1" if pid == "p1" else "wf-PROJ-2", "workflow_type": "implement_jira_ticket", "status": "succeeded"}]}
        response = self.client.get("/admin/metrics?days=30&project=PROJ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["jira_issue_key"] for r in response.json()["tickets"]], ["PROJ-1"])

    def test_a_role_without_view_is_refused(self):
        self.auth = AuthContext(actor_id="ada", tenant_id="default", roles=["nobody"], auth_provider="oidc")
        self.assertEqual(self.client.get("/admin/metrics").status_code, 403)


if __name__ == "__main__":
    unittest.main()
