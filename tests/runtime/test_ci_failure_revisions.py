"""Failing CI goes back to OpenCode with the job log, and a failed coding run can be run again."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ts_harness.integrations.github_client import GitHubClient
from ts_harness.workflows import human_decisions
from ts_harness.workflows.engine import _qa_feedback_text
from ts_harness.workflows.hooks.wait_external import _failed_check_logs
from ts_harness.workflows.materialize import load_skeleton

REPO_ROOT = Path(__file__).resolve().parents[2]
PR = "https://github.com/acme/app/pull/25"
CHECKS_FAILED = {
    "checks_state": "failure",
    "failed_checks": [f"{PR}:test"],
    "prs": [{"pr_url": PR, "state": "failure", "failed": ["test"], "head_sha": "abc123"}],
}


class FailedChecksTests(unittest.TestCase):
    def test_the_checks_wait_revises_the_coding_stage_on_its_own_budget(self):
        stage = load_skeleton("deliver_work_order").stage("await_pr_checks")
        self.assertEqual(stage.on_block["hook"], "revise_on_reject")
        self.assertEqual(stage.on_block["revise"]["step_name"], "deliver_work_order")
        self.assertEqual(stage.on_block["revise"]["budget"], "checks")
        self.assertEqual(stage.on_block["revise"]["input_map"]["qa_feedback"], "$reject.comments")
        self.assertEqual(human_decisions.available_decisions(stage), ["revise", "stop"])

    def test_feedback_names_the_checks_and_quotes_the_log(self):
        output = {**CHECKS_FAILED, "failed_check_logs": {f"{PR}:test": "Error: Cannot bundle node:sqlite"}}
        text = _qa_feedback_text({}, {"output": output}, "external_blocked:github.pr_checks x")
        self.assertIn(f"CI checks failed: {PR}:test", text)
        self.assertIn("Cannot bundle node:sqlite", text)
        self.assertIn("not only locally", text)

    def test_logs_are_fetched_for_failed_jobs_only(self):
        client = MagicMock()
        client.failed_check_logs.return_value = {"test": "tail"}
        with patch("ts_harness.integrations.github_client.github_for", return_value=client):
            logs = _failed_check_logs(CHECKS_FAILED)
        self.assertEqual(logs, {f"{PR}:test": "tail"})
        client.failed_check_logs.assert_called_once_with("acme", "app", "abc123", ["test"])

    def test_a_log_lookup_error_never_stops_the_block(self):
        with patch("ts_harness.integrations.github_client.github_for", side_effect=RuntimeError("no token")):
            self.assertEqual(_failed_check_logs(CHECKS_FAILED), {})

    def test_job_log_tail_strips_colours_and_timestamps(self):
        raw = b"2026-11-14T20:26:10.95Z \x1b[31mFAIL\x1b[39m tests/a.test.js\n\n2026-11-14T20:26:11Z Process completed with exit code 1.\n"
        client = GitHubClient(token="t")
        with patch("ts_harness.integrations.github_client._download_job_log", return_value=raw):
            self.assertEqual(client.job_log_tail("acme", "app", 1), "FAIL tests/a.test.js\nProcess completed with exit code 1.")
        with patch("ts_harness.integrations.github_client._download_job_log", side_effect=OSError("gone")):
            self.assertEqual(client.job_log_tail("acme", "app", 1), "")

    def test_failed_check_logs_reads_only_failed_named_runs(self):
        client = GitHubClient(token="t")
        runs = [{"id": 1, "name": "test", "conclusion": "failure"}, {"id": 2, "name": "lint", "conclusion": "success"},
                {"id": 3, "name": "build", "conclusion": "failure"}]
        with patch.object(client, "check_runs", return_value=runs), patch.object(client, "job_log_tail", return_value="tail") as tail:
            self.assertEqual(client.failed_check_logs("acme", "app", "abc", ["test", "lint"]), {"test": "tail"})
        tail.assert_called_once_with("acme", "app", 1)


class FailedCodingRunTests(unittest.TestCase):
    def test_a_failed_coding_run_can_be_run_again_or_stopped(self):
        stage = load_skeleton("deliver_work_order").stage("deliver_work_order")
        self.assertEqual(human_decisions.available_decisions(stage), ["revise", "stop"])

    def test_revise_re_runs_the_step_with_the_error_and_guidance(self):
        task = {"id": "t-1", "step_name": "deliver_work_order", "status": "blocked", "workflow_type": "deliver_work_order",
                "error": "/pr_url: deliver_work_order cannot succeed when run_coding_backend failed: opencode exit -9",
                "input": {"work_order_id": "WO-003"}, "output": {}}
        engine = MagicMock()
        engine.client.get_workflow.return_value = {"workflow": {"workflow_type": "deliver_work_order"}, "tasks": [task]}
        engine._context.return_value = SimpleNamespace(stage=load_skeleton("deliver_work_order").stage("deliver_work_order"),
                                                       extras={}, reject_comments="")
        result = human_decisions.decide(engine, "wf-1", "t-1", "revise", actor="pm@acme.test", guidance="Keep it under src/reminder-worker/.")
        self.assertEqual((result["status"], result["rerun"], result["work_order"]), ("revising", True, "WO-003"))
        kwargs = engine.client.resume_blocked_task.call_args.kwargs
        note = kwargs["input_patch"]["qa_feedback"]
        self.assertIn("opencode exit -9", note)
        self.assertIn("Keep it under src/reminder-worker/.", note)
        engine.registry.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
