"""Turning a finished coding job into the deliver stage result."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from ts_harness.workflows import coding_job_stage

PR = "https://github.com/acme/app/pull/7"
CONTRACT = {
    "output_schema": {
        "type": "object",
        "required": ["work_order_id", "pr_url"],
        "properties": {"work_order_id": {"type": "string", "minLength": 1}, "pr_url": {"type": "string", "pattern": "^https://"}},
    },
    "required_artifacts": {"pr_url": "/pr_url"},
    "semantic_validators": ["validate_coding_backend_pr_url"],
}


def job(status, result=None, error=None):
    return {"id": "job-1", "status": status, "result": result, "error": error}


class DispatchResultTests(unittest.TestCase):
    def test_success_passes_the_contract_and_carries_trusted_fields(self):
        run = {"ok": True, "work_order_id": "WO-1", "pr_url": PR, "branch": "b", "head_sha": "abc", "security": {"x": 1}, "summary": "done"}
        result = coding_job_stage.dispatch_result(job("succeeded", run), CONTRACT, {})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"]["pr_url"], PR)
        self.assertEqual((result["output"]["head_sha"], result["output"]["security"]), ("abc", {"x": 1}))
        self.assertEqual(result["coding_job_id"], "job-1")

    def test_success_without_a_pr_url_fails_the_contract(self):
        run = {"ok": True, "work_order_id": "WO-1"}
        result = coding_job_stage.dispatch_result(job("succeeded", run), CONTRACT, {})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("pr_url", result["error"])

    def test_failed_run_blocks_with_its_error(self):
        result = coding_job_stage.dispatch_result(job("failed", {"ok": False, "error": "lint failed"}), CONTRACT, {})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"], "deliver_work_order cannot succeed when run_coding_backend failed: lint failed")

    def test_lost_job_is_a_transport_error_so_it_retries(self):
        result = coding_job_stage.dispatch_result(job("lost", {"ok": False, "error": "coding job lost: no heartbeat"}), CONTRACT, {})
        self.assertEqual(result["error"], "transport_error: coding job lost: no heartbeat")

    def test_cancelled_job(self):
        result = coding_job_stage.dispatch_result(job("cancelled", error="workflow cancelled"), CONTRACT, {})
        self.assertEqual((result["status"], result["error"]), ("blocked", "coding job cancelled: workflow cancelled"))

    def test_without_a_contract_ok_decides(self):
        self.assertEqual(coding_job_stage.dispatch_result(job("succeeded", {"ok": True}), None, {})["status"], "succeeded")
        self.assertEqual(coding_job_stage.dispatch_result(job("failed"), None, {})["status"], "blocked")


class StageSpecTests(unittest.TestCase):
    def test_refresh_mode_and_bound_inputs(self):
        task = {"id": "t", "workflow_id": "wf-1", "agent_id": "dev-agent"}
        task_input = {"work_order_id": "WO-1", "repo_url": "https://github.com/acme/app", "pr_url": PR, "refresh": True, "requested_by": "a@corp.com"}
        with mock.patch.object(coding_job_stage, "build_coding_request", return_value={"repo_url": "x"}) as build:
            coding_job_stage.stage_spec(task, task_input, program_id="prog-1")
        args = build.call_args.args[0]
        kwargs = build.call_args.kwargs
        self.assertEqual((args["mode"], args["workflow_id"]), ("refresh", "wf-1"))
        self.assertEqual((kwargs["delivery_workflow_id"], kwargs["program_id"], kwargs["requested_by"]), ("wf-1", "prog-1", "a@corp.com"))
        self.assertEqual(kwargs["bound"]["pr_url"], PR)
        self.assertTrue(kwargs["bound"]["refresh"])

    def test_implement_mode_by_default(self):
        with mock.patch.object(coding_job_stage, "build_coding_request", return_value={}) as build:
            coding_job_stage.stage_spec({"id": "t", "workflow_id": "wf"}, {"work_order_id": "WO-1"}, program_id=None)
        self.assertEqual(build.call_args.args[0]["mode"], "implement")

    def test_enabled_flag(self):
        with mock.patch.dict(os.environ, {"CODING_JOBS_ENABLED": "true"}):
            self.assertTrue(coding_job_stage.enabled())
        with mock.patch.dict(os.environ, {"CODING_JOBS_ENABLED": ""}):
            self.assertFalse(coding_job_stage.enabled())


if __name__ == "__main__":
    unittest.main()
