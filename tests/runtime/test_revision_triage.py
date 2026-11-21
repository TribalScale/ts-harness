"""Triage at the revision limit: continue, accept with follow-ups, or ask a person."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows import revision_triage as rt
from ts_harness.workflows.hooks import revise_on_reject
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext

MAJOR = {"file": "src/reports/export.ts", "line": 40, "summary": "CSV export drops the last row", "in_scope": True,
         "severity": "major", "security": False, "clear": True}
MINOR = {**MAJOR, "summary": "rename a variable", "severity": "minor"}
TRIAGE = {"progress": True, "open_findings": [MAJOR], "guidance": "Include the final row in export.ts and test a 3-row export.",
          "follow_ups": [], "reason": "new findings each round"}


class PolicyTests(unittest.TestCase):
    def decide(self, triage, autonomy="autonomous", total=3, findings=None):
        return rt.decide(triage, autonomy=autonomy, total=total, open_findings=findings or [{"message": "export drops a row"}])["decision"]

    def test_the_policy(self):
        self.assertEqual(self.decide(TRIAGE), "continue")
        self.assertEqual(self.decide(TRIAGE, autonomy="standard"), "continue")
        self.assertEqual(self.decide(TRIAGE, autonomy="conservative"), "escalate")
        self.assertEqual(self.decide(TRIAGE, total=8), "escalate")
        self.assertEqual(self.decide(None), "escalate")
        self.assertEqual(self.decide({**TRIAGE, "progress": False}), "escalate")
        self.assertEqual(self.decide({**TRIAGE, "open_findings": [{**MAJOR, "clear": False}]}), "escalate")
        # Security always goes to a person: the model's flag, or the finding's own words.
        self.assertEqual(self.decide({**TRIAGE, "open_findings": [{**MAJOR, "security": True}]}), "escalate")
        self.assertEqual(self.decide(TRIAGE, findings=[{"message": "JWKS fetch failures look like invalid tokens"}]), "escalate")
        # Only minor findings left: autonomous accepts, standard asks.
        minor_only = {**TRIAGE, "open_findings": [MINOR], "follow_ups": [{"title": "Rename", "description": "x"}]}
        self.assertEqual(self.decide(minor_only), "accept")
        self.assertEqual(self.decide(minor_only, autonomy="standard"), "escalate")

    def test_total_revisions_counts_every_grant_but_not_budgeted_runs(self):
        tasks = [{"step_name": "deliver_work_order", "status": "succeeded", "input": {}} for _ in range(5)]
        tasks.append({"step_name": "deliver_work_order", "status": "succeeded", "input": {"_revision_budget": "checks"}})
        tasks.append({"step_name": "deliver_work_order", "status": "cancelled", "input": {}})
        self.assertEqual(rt.total_revisions(tasks, "deliver_work_order"), 4)


def _review(i, status="cancelled", file="src/reports/export.ts", line=40):
    return {"id": f"finalize-{i}", "step_name": "finalize_code_review", "status": status, "created_at": f"2026-01-0{i}5",
            "output": {"verdict": "block", "findings": [{"severity": "major", "file": file, "line": line, "message": f"issue {i}"}]}}


def _deliver(i, **extra):
    return {"id": f"deliver-{i}", "step_name": "deliver_work_order", "status": "succeeded", "created_at": f"2026-01-0{i}0",
            "input": {"work_order_id": "WO-1", "work_order_body": "Reports export.", "jira_issue_key": "PROJ-5", **extra},
            "output": {"pr_url": "https://github.com/org/api/pull/9", "summary": f"attempt {i}"}}


class LoopTests(unittest.TestCase):
    def _ctx(self, tasks):
        skeleton = load_skeleton("deliver_work_order")
        gate = next(t for t in tasks if t["step_name"] == "finalize_code_review" and t["status"] == "blocked")
        ctx = HookContext(workflow_id="wf-1", workflow_type="deliver_work_order", tenant_id="default", task=gate,
                          stage=skeleton.stage("finalize_code_review"), skeleton=skeleton, actor="orchestrator",
                          memory_client=MagicMock(), reject_comments="fix it", tasks=tasks, workflow={}, runner_event="blocked",
                          extras={"error": "barrier_blocked: code_review_blocked"})
        ctx.memory_client.get_workflow.return_value = {"workflow": {}, "tasks": tasks}
        return ctx

    def _at_limit(self):
        # Four review rounds, each a different place: progress, not a repeat, and the budget is used up.
        tasks = [_deliver(1), _review(1, line=10)]
        for i, line in ((2, 200), (3, 400), (4, 600)):
            tasks += [_deliver(i, _revision_source="finalize_code_review", review_feedback="x"),
                      _review(i, "blocked" if i == 4 else "cancelled", line=line)]
        return tasks

    @patch("ts_harness.workflows.revision_guidance.guided_coding_model", return_value="anthropic/claude-opus-5-5")
    @patch("ts_harness.workflows.revision_triage.project_autonomy", return_value="autonomous")
    @patch("ts_harness.workflows.revision_triage.rg.diagnosis_model", return_value="m")
    @patch("ts_harness.workflows.revision_triage.rg.build_model_call", return_value=lambda prompt: json.dumps(TRIAGE))
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_progress_at_the_limit_continues_on_the_stronger_model(self, bulk, *_):
        bulk.side_effect = [[f"t{i}"] for i in range(20)]
        result = revise_on_reject.handle(self._ctx(self._at_limit()))
        self.assertEqual((result["status"], result.get("auto_grant")), ("revising", True))
        revise = bulk.call_args_list[0].kwargs["tasks"][0]
        self.assertTrue(revise["input"]["_auto_grant"])
        self.assertEqual(revise["input"]["coding_model"], "anthropic/claude-opus-5-5")
        self.assertIn("Include the final row", revise["input"]["review_feedback"])

    @patch("ts_harness.workflows.revision_triage.project_autonomy", return_value="autonomous")
    @patch("ts_harness.workflows.revision_triage.rg.diagnosis_model", return_value="m")
    @patch("ts_harness.workflows.revision_triage.rg.build_model_call",
           return_value=lambda prompt: json.dumps({**TRIAGE, "open_findings": [MINOR], "follow_ups": [{"title": "Rename", "description": "d"}]}))
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_only_minor_findings_left_is_accepted(self, bulk, *_):
        result = revise_on_reject.handle(self._ctx(self._at_limit()))
        self.assertEqual(result["status"], "auto_accept")
        self.assertEqual(result["triage"]["triage"]["follow_ups"][0]["title"], "Rename")
        bulk.assert_not_called()

    @patch("ts_harness.workflows.revision_triage.project_autonomy", return_value="conservative")
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_a_conservative_project_asks(self, bulk, _):
        result = revise_on_reject.handle(self._ctx(self._at_limit()))
        self.assertEqual(result["status"], "revision_limit")
        self.assertIn("asks a person", result["triage"]["reason"])
        bulk.assert_not_called()

    @patch("ts_harness.workflows.revision_triage.project_autonomy", return_value="autonomous")
    @patch("ts_harness.workflows.revision_triage.rg.diagnosis_model", return_value="m")
    @patch("ts_harness.workflows.revision_triage.rg.build_model_call", return_value=lambda prompt: json.dumps(TRIAGE))
    @patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk")
    def test_eight_revisions_is_the_ceiling(self, bulk, *_):
        tasks = self._at_limit()
        tasks = [t for t in tasks if not (t["step_name"] == "finalize_code_review" and t["status"] == "blocked")]
        for i in (5, 6, 7, 8, 9):
            tasks += [_deliver(i, _revision_source="finalize_code_review", review_feedback="x", _auto_grant=True),
                      _review(i, "blocked" if i == 9 else "cancelled", line=100 * i)]
        result = revise_on_reject.handle(self._ctx(tasks))
        self.assertEqual(result["status"], "revision_limit")
        self.assertIn("8 revisions", result["triage"]["reason"])


class FollowUpTests(unittest.TestCase):
    def test_follow_ups_go_under_the_epic_and_link_to_the_ticket(self):
        jira = MagicMock()
        jira.get_issue.return_value = {"fields": {"parent": {"key": "PROJ-1"}, "issuetype": {"name": "Story"}}}
        jira.create_issue.side_effect = [{"key": "PROJ-40"}, {"key": "PROJ-41"}]
        keys = rt.file_follow_ups(jira, ticket_key="PROJ-5", follow_ups=[{"title": "A", "description": "a"}, {"title": "B"}, {"title": ""}])
        self.assertEqual(keys, ["PROJ-40", "PROJ-41"])
        body = jira.create_issue.call_args_list[0].args[0]
        self.assertEqual((body["project"], body["parent"], body["summary"]), ({"key": "PROJ"}, {"key": "PROJ-1"}, "A"))
        jira.link_issues.assert_any_call("Relates", inward_key="PROJ-40", outward_key="PROJ-5")


class ProjectSettingTests(unittest.TestCase):
    def test_autonomy_is_read_from_the_programs_project(self):
        memory = MagicMock()
        memory.get_program.return_value = {"program": {"project_id": "PAY", "tenant_id": "default"}}
        memory.list_projects.return_value = {"projects": [{"key": "PAY", "revision_autonomy": "standard"}]}
        self.assertEqual(rt.project_autonomy(memory, {"program_id": "p1"}), "standard")
        memory.list_projects.return_value = {"projects": [{"key": "PAY"}]}
        self.assertEqual(rt.project_autonomy(memory, {"program_id": "p1"}), "autonomous")
        self.assertEqual(rt.project_autonomy(MagicMock(), {}), "autonomous")

    def test_the_memory_api_validates_the_setting(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "memory_routes" / "projects.py"
        spec = importlib.util.spec_from_file_location("projects_routes", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module._revision_autonomy(None), "autonomous")
        self.assertEqual(module._revision_autonomy("Standard"), "standard")
        with self.assertRaises(ValueError):
            module._revision_autonomy("yolo")


if __name__ == "__main__":
    unittest.main()
