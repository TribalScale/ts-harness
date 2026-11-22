"""scripts/ops/e2e-regression.py: decisions from fake API responses, safety refusals, the report."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "e2e-regression.py"
spec = importlib.util.spec_from_file_location("e2e_regression", SCRIPT)
e2e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e2e  # dataclasses resolve annotations through the module
spec.loader.exec_module(e2e)

REPO = "example-org/bookshelf-sandbox"
ENV = {
    "E2E_SANDBOX": "1",
    "E2E_GATEWAY_URL": "https://staging.example.test/",
    "E2E_TOKEN": "service-token",
    "E2E_JIRA_PROJECT": "DEMO",
    "E2E_REPO": REPO,
    "E2E_ACTOR": "e2e@example.com",
    "E2E_GITHUB_TOKEN": "gh-token",
    "E2E_SANDBOX_REPOS": f"{REPO},example-org/other-sandbox",
    "E2E_SANDBOX_JIRA_PROJECTS": "DEMO",
}
PRD = "# Bookshelf\n\nAdd a book."


def cfg(**overrides):
    return e2e.config_from_env({**ENV, **overrides})


def gate(task_id, program_id, title="review_prd"):
    return {"type": "workflow_gate", "id": task_id, "title": title,
            "meta": {"program_id": program_id, "task_id": task_id, "step_name": title}}


def proposal(session_id, key="start_program"):
    return {"type": "pending_proposal", "id": f"{session_id}:{key}", "title": "Start the software_delivery program",
            "meta": {"session_id": session_id, "key": key, "status": "pending"}}


def live(program_id, workflow_id, *, state="OpenCode implementing", step="deliver_work_order", pr="", kind="running"):
    return {"program_id": program_id, "workflow_id": workflow_id, "jira_issue_key": "DEMO-1", "label": "DEMO-1",
            "state": state, "current_step": step, "kind": kind, "pr_url": pr, "revisions": 0, "elapsed_seconds": 60}


def kinds(actions):
    return [(a.kind, a.value) for a in actions]


class SafetyTests(unittest.TestCase):
    def test_complete_sandbox_config_passes(self):
        self.assertEqual(e2e.safety_errors(cfg()), [])

    def test_refuses_without_sandbox_flag(self):
        errors = e2e.safety_errors(cfg(E2E_SANDBOX="0"))
        self.assertTrue(any("E2E_SANDBOX=1" in error for error in errors))

    def test_refuses_repo_not_in_sandbox_list(self):
        errors = e2e.safety_errors(cfg(E2E_REPO="example-org/production-app"))
        self.assertTrue(any("not a configured sandbox repository" in error for error in errors))

    def test_refuses_jira_project_not_in_sandbox_list(self):
        errors = e2e.safety_errors(cfg(E2E_JIRA_PROJECT="LIVE"))
        self.assertTrue(any("not a configured sandbox project" in error for error in errors))

    def test_refuses_without_sandbox_lists(self):
        errors = e2e.safety_errors(cfg(E2E_SANDBOX_REPOS="", E2E_SANDBOX_JIRA_PROJECTS=""))
        self.assertEqual(sum("must list" in error for error in errors), 2)

    def test_reports_missing_settings_without_values(self):
        errors = e2e.safety_errors(cfg(E2E_TOKEN="", E2E_GITHUB_TOKEN=""))
        self.assertIn("missing settings: E2E_TOKEN, E2E_GITHUB_TOKEN", errors)

    def test_main_exits_2_and_never_prints_the_token(self):
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = e2e.main([], env={**ENV, "E2E_SANDBOX": "", "E2E_TOKEN": "secret-value-123"})
        self.assertEqual(code, 2)
        self.assertNotIn("secret-value-123", err.getvalue() + out.getvalue())

    def test_github_client_refuses_other_repositories(self):
        calls = []
        github = e2e.GitHub(cfg(), request=lambda *a, **k: calls.append(a) or {"merged": True})
        with self.assertRaises(e2e.ConfigError):
            github.merge("https://github.com/example-org/production-app/pull/4")
        self.assertEqual(calls, [])
        self.assertEqual(github.merge(f"https://github.com/{REPO}/pull/4"), "merged")
        self.assertEqual(calls[0][0], "PUT")


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg()
        self.state = e2e.RunState(started_at=1000.0, baseline={"old-program"})

    def snap(self, now=1100.0, **kwargs):
        snap = e2e.Snapshot(now=now, **kwargs)
        e2e.adopt_programs(self.state, snap)
        return snap

    def step(self, snap):
        actions = e2e.decide(self.state, snap, self.cfg, PRD)
        for action in actions:
            if action.kind != "wait":
                e2e.apply(self.state, action, snap.now, result="merged" if action.kind == "merge" else "")
        return actions

    def test_start_sends_the_prd_and_enters_planning(self):
        actions = self.step(self.snap())
        self.assertEqual(actions[0].kind, "send")
        self.assertIn("Add a book.", actions[0].value)
        self.assertIn("DEMO", actions[0].value)
        self.assertEqual(self.state.stage, "planning")

    def test_full_happy_path(self):
        self.step(self.snap())
        self.state.session_id = "s-1"
        # The chat offers to start the program: answered once.
        actions = self.step(self.snap(inbox=[proposal("s-1"), proposal("someone-else")]))
        self.assertEqual(kinds(actions), [("send", "yes"), ("answered", "s-1:start_program")])
        self.assertEqual(kinds(self.step(self.snap(inbox=[proposal("s-1")]))), [("wait", "waiting for the program to start")])
        # The program appears; its PRD gate is approved; old programs' gates are left alone.
        programs = {"p-plan": {"status": "running", "phases": {"prd": {"state": "running"}}}}
        snap = self.snap(program_ids=["old-program", "p-plan"], programs=programs,
                         inbox=[gate("t-prd", "p-plan"), gate("t-old", "old-program")])
        self.assertEqual(self.state.planning_programs, ["p-plan"])
        self.assertEqual(kinds(self.step(snap)), [("approve", "t-prd")])
        self.assertEqual(kinds(self.step(snap)), [("wait", "planning")])
        # PRD approved: choose the track once.
        programs["p-plan"]["phases"]["prd"]["state"] = "succeeded"
        actions = self.step(self.snap(program_ids=["p-plan"], programs=programs))
        self.assertEqual(kinds(actions), [("send", "file work orders in jira project DEMO"), ("track_sent", "")])
        # Planning gates (Refinery, Foundry, Planner) each approved.
        for task in ("t-refinery", "t-foundry", "t-planner"):
            self.assertEqual(kinds(self.step(self.snap(program_ids=["p-plan"], programs=programs,
                                                       inbox=[gate(task, "p-plan", task)]))), [("approve", task)])
        # Filing done: implement all.
        programs["p-plan"] = {"status": "succeeded", "phases": {"file_jira": {"state": "succeeded"}}}
        self.assertEqual(kinds(self.step(self.snap(now=2000, program_ids=["p-plan"], programs=programs))),
                         [("stage", "implement_start")])
        self.assertEqual(kinds(self.step(self.snap(now=2001))), [("send", "implement all DEMO"), ("stage", "implementing")])
        self.assertEqual(self.state.stage_times["planning"]["end"], 2000)
        # A new program after "implement all" is the implement program; its queue gate is approved.
        programs["p-impl"] = {"status": "running"}
        snap = self.snap(now=2100, program_ids=["p-plan", "p-impl"], programs=programs,
                         inbox=[proposal("s-1", "queue"), gate("t-queue", "p-impl", "confirm_queue")])
        self.assertEqual(self.state.implement_programs, ["p-impl"])
        self.assertEqual(kinds(self.step(snap)), [("send", "yes"), ("answered", "s-1:queue"), ("approve", "t-queue")])
        # Coding, then Ready to merge: merged once.
        url = f"https://github.com/{REPO}/pull/7"
        snap = self.snap(now=2200, program_ids=["p-impl"], programs=programs, live=[live("p-impl", "wf-1", pr=url)])
        e2e.observe(self.state, snap)
        self.assertEqual(self.step(snap)[0].kind, "wait")
        snap = self.snap(now=2300, programs=programs,
                         live=[live("p-impl", "wf-1", pr=url, state="Ready to merge", step="await_human_merge")])
        self.assertEqual(kinds(self.step(snap)), [("merge", url)])
        self.assertEqual(self.state.prs[url], "merged")
        self.assertEqual(self.step(snap)[0].kind, "wait")
        # Program finished and nothing in flight: done.
        programs["p-impl"] = {"status": "succeeded"}
        self.assertEqual(kinds(self.step(self.snap(now=2400, programs=programs))), [("stage", "done")])
        self.assertEqual(self.state.stage, "done")
        self.assertEqual(self.state.stage_times["implementing"], {"start": 2001, "end": 2400})
        self.assertEqual(e2e.decide(self.state, self.snap(now=2500), self.cfg, PRD), [])

    def _planning(self):
        self.step(self.snap())
        self.state.session_id = "s-1"
        self.state.planning_programs = ["p-plan"]

    def test_stuck_step_fails_the_run(self):
        self._planning()
        stuck = {"type": "stuck_step", "id": "t-9", "title": "WO-1 · code_review",
                 "subtitle": "Revision limit reached", "meta": {"program_id": "p-plan"}}
        actions = self.step(self.snap(inbox=[stuck], programs={"p-plan": {"status": "running"}}))
        self.assertEqual(actions[0].kind, "fail")
        self.assertIn("stuck step", self.state.failures[0])

    def test_failed_program_and_failed_ticket_fail_the_run(self):
        self._planning()
        actions = self.step(self.snap(programs={"p-plan": {"status": "failed"}},
                                      live=[live("p-plan", "wf-2", kind="failed", state="Failed")]))
        self.assertEqual([a.kind for a in actions], ["fail", "fail"])

    def test_pr_outside_sandbox_is_never_merged(self):
        self._planning()
        url = "https://github.com/example-org/production-app/pull/3"
        actions = self.step(self.snap(programs={"p-plan": {"status": "running"}},
                                      live=[live("p-plan", "wf-3", pr=url, state="Ready to merge")]))
        self.assertEqual([a.kind for a in actions], ["fail"])
        self.assertNotIn(url, self.state.prs)

    def test_global_timeout(self):
        self._planning()
        actions = self.step(self.snap(now=1000 + self.cfg.timeout_minutes * 60 + 1))
        self.assertEqual(actions[0].kind, "fail")
        self.assertIn("global timeout", actions[0].value)

    def test_no_program_after_grace_fails(self):
        self.step(self.snap())
        actions = self.step(self.snap(now=1000 + self.cfg.start_grace_minutes * 60 + 1))
        self.assertEqual(actions[-1].kind, "fail")

    def test_failed_merge_is_recorded_and_not_retried(self):
        self._planning()
        self.state.stage = "implementing"
        self.state.implement_programs = ["p-impl"]
        url = f"https://github.com/{REPO}/pull/9"
        snap = self.snap(programs={"p-impl": {"status": "running"}},
                         live=[live("p-impl", "wf-4", pr=url, state="Ready to merge")])
        merge = e2e.decide(self.state, snap, self.cfg, PRD)[0]
        e2e.apply(self.state, merge, snap.now, result="HTTP 405: not mergeable")
        self.assertEqual(self.state.prs[url], "merge_failed")
        self.assertIn("not mergeable", self.state.failures[0])


class ReportTests(unittest.TestCase):
    def _state(self):
        state = e2e.RunState(started_at=0.0, stage="done", session_id="s-1",
                             planning_programs=["p-plan"], implement_programs=["p-impl"])
        state.stage_times = {"planning": {"start": 0, "end": 600}, "implementing": {"start": 600, "end": 3000}}
        url = f"https://github.com/{REPO}/pull/7"
        state.prs = {url: "merged"}
        state.tickets = {"wf-1": {"program_id": "p-impl", "jira_issue_key": "DEMO-1", "pr_url": url}}
        return state

    def test_passing_report(self):
        history = [{"program_id": "p-impl", "workflow_id": "wf-1", "outcome": "merged",
                    "pr_url": f"https://github.com/{REPO}/pull/7"}]
        report = e2e.build_report(self._state(), finished_at=3100, history=history, costs={"DEMO-1": 1.25},
                                  jira_keys=["DEMO-1"], cfg=cfg())
        self.assertTrue(report["ok"], report["checks"])
        self.assertEqual(report["total_seconds"], 3100)
        self.assertEqual(report["cost_usd"], 1.25)
        self.assertEqual(report["stages"]["planning"], {"seconds": 600, "completed": True})
        self.assertNotIn("service-token", json.dumps(report))
        self.assertNotIn("gh-token", json.dumps(report))

    def test_lost_ticket_and_unmerged_pr_fail(self):
        state = self._state()
        state.tickets["wf-2"] = {"program_id": "p-impl", "jira_issue_key": "DEMO-2"}
        state.prs[f"https://github.com/{REPO}/pull/8"] = "open"
        history = [{"program_id": "p-impl", "workflow_id": "wf-1", "outcome": "merged"},
                   {"program_id": "p-impl", "workflow_id": "wf-2", "outcome": "failed"}]
        report = e2e.build_report(state, finished_at=3100, history=history, costs={}, jira_keys=[], cfg=cfg())
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["no_lost_coding_jobs"])
        self.assertFalse(report["checks"]["prs_merged"])
        self.assertEqual(report["lost_tickets"], ["wf-2"])

    def test_incomplete_stage_fails(self):
        state = self._state()
        state.stage = "implementing"
        state.stage_times["implementing"] = {"start": 600}
        state.failures = ["global timeout after 240 minutes in stage implementing"]
        report = e2e.build_report(state, finished_at=9000, history=[], costs={}, jira_keys=["DEMO-1"], cfg=cfg())
        self.assertFalse(report["checks"]["implementing_completed"])
        self.assertFalse(report["checks"]["no_stuck_or_failed_steps"])
        markdown = e2e.render_markdown(report)
        self.assertIn("# E2E regression: FAIL", markdown)
        self.assertIn("global timeout", markdown)

    def test_write_report_files(self):
        report = e2e.build_report(self._state(), finished_at=3100, history=[], costs={}, jira_keys=[], cfg=cfg())
        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = e2e.write_report(report, Path(tmp) / "out")
            self.assertEqual(json.loads(json_path.read_text())["stage"], "done")
            self.assertIn("## Checks", md_path.read_text())


class DriverTests(unittest.TestCase):
    """The loop and the clients against an in-memory fake API (no network)."""

    def test_run_loop_with_fake_api(self):
        url = f"https://github.com/{REPO}/pull/1"
        world = {"tick": 0, "approved": [], "sent": [], "merged": []}

        def fake(method, full_url, *, headers, body=None, timeout=60):
            path = full_url.split("staging.example.test", 1)[-1].split("api.github.com", 1)[-1]
            tick = world["tick"]
            if path == "/session/message":
                world["sent"].append(body["text"])
                assert body["channel"] == "web" and body["actor"]["email"] == "e2e@example.com"
                return {"status": "succeeded", "session_id": "s-1"}
            if path.startswith("/admin/programs?"):
                ids = ["old"] + (["p-plan"] if tick >= 1 else []) + (["p-impl"] if "implement all DEMO" in world["sent"] else [])
                return {"programs": [{"id": i} for i in ids]}
            if path == "/admin/programs/p-plan/status":
                done = "t-plan" in world["approved"]
                return {"status": "succeeded" if done and len(world["sent"]) >= 2 else "running",
                        "phases": {"prd": {"state": "succeeded" if done else "running"}}}
            if path == "/admin/programs/p-impl/status":
                return {"status": "succeeded" if world["merged"] else "running"}
            if path == "/admin/inbox":
                return {"items": [gate("t-plan", "p-plan")] if tick >= 1 and "t-plan" not in world["approved"] else []}
            if path == "/admin/live":
                if "implement all DEMO" in world["sent"] and not world["merged"]:
                    return {"items": [live("p-impl", "wf-1", pr=url, state="Ready to merge")]}
                return {"items": []}
            if path == "/admin/workflows/approve":
                world["approved"].append(body["id"])
                return {"status": "approved"}
            if path == f"/repos/{REPO}/pulls/1/merge":
                assert headers["Authorization"] == "Bearer gh-token"
                world["merged"].append(url)
                return {"merged": True}
            raise AssertionError(f"unexpected {method} {path}")

        config = cfg(E2E_POLL_SECONDS="1")
        clock = iter(range(0, 100000, 10))

        def sleep(_seconds):
            world["tick"] += 1

        state = e2e.run(config, e2e.Gateway(config, request=fake), e2e.GitHub(config, request=fake),
                        clock=lambda: float(next(clock)), sleep=sleep, say=lambda _msg: None)
        self.assertEqual(state.failures, [])
        self.assertEqual(state.stage, "done")
        self.assertEqual(world["approved"], ["t-plan"])
        self.assertEqual(world["merged"], [url])
        self.assertEqual(world["sent"][-1], "implement all DEMO")
        self.assertEqual(state.prs, {url: "merged"})


if __name__ == "__main__":
    unittest.main()
