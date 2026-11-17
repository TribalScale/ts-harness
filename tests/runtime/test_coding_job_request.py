"""The extracted coding request builder, spec round-trip and stage output."""

from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.implementation import coding_job
from ts_harness.implementation.backends.base import CodingRequest, CodingResult
from ts_harness.implementation.coding_job import (
    CodingRequestError,
    build_coding_request,
    coding_request_from_spec,
    coding_stage_output,
    run_coding_request,
)
from ts_harness.tools.coding_backend_tool import handle_run_coding_backend
from ts_harness.tools.policy import ToolPolicyContext

PR = "https://github.com/example/app/pull/7"


def configured(stack: ExitStack, *, coding_backend: str = "opencode", backend: object | None = None) -> None:
    live = MagicMock()
    live.identity.github_logins = {"alice@example.com": "alice-gh"}
    live.program_defaults = None
    stack.enter_context(patch.object(coding_job, "load_active_runtime_config", return_value=MagicMock()))
    stack.enter_context(patch.object(coding_job, "_live_runtime_config", return_value=live))
    stack.enter_context(patch.object(coding_job, "check_repo_allowed", return_value=None))
    stack.enter_context(
        patch.object(
            coding_job,
            "implementation_config",
            return_value=MagicMock(coding_backend=coding_backend, tools_root="/tools"),
        )
    )
    stack.enter_context(
        patch.object(
            coding_job,
            "repo_for_workflow",
            return_value=MagicMock(url="https://github.com/example/app.git", base_branch="dev", clone_depth=2),
        )
    )
    stack.enter_context(
        patch.object(
            coding_job,
            "opencode_backend_config",
            return_value=MagicMock(
                provider="anthropic", model="claude", binary="opencode", run_args=["run"], verification_fix_passes=1
            ),
        )
    )
    if backend is not None:
        stack.enter_context(patch.object(coding_job, "OpenCodeBackend", backend))


class BuildCodingRequestTests(unittest.TestCase):
    def test_spec_is_json_safe_and_round_trips(self):
        with ExitStack() as stack:
            configured(stack)
            spec = build_coding_request(
                {
                    "work_order_id": "WO-002",
                    "work_order_body": "# WO-002\nbody",
                    "base_branch": "main",
                    "review_feedback": "fix the test\x00",
                    "revision_files": ["a.py", " ", "b.py"],
                    "workflow_id": "wf-impl",
                },
                delivery_workflow_id="12345678-aaaa-bbbb-cccc-1234567890ab",
                program_id="prog-1",
                requested_by="Alice@Example.com",
            )
        self.assertEqual(json.loads(json.dumps(spec)), spec)
        self.assertEqual(spec["base_branch"], "dev")
        self.assertEqual(spec["clone_depth"], 2)
        self.assertEqual(spec["requested_by"], "alice@example.com")
        self.assertEqual(spec["requested_by_github_login"], "alice-gh")
        self.assertEqual(spec["review_feedback"], "fix the test")
        self.assertEqual(spec["revision_files"], ["a.py", "b.py"])
        self.assertEqual(spec["record_workflow_id"], "wf-impl")
        self.assertTrue(spec["workspace_dir"].startswith("/workspace/dev-agent/coding-backend/WO-002-"))

        request = coding_request_from_spec(spec)
        self.assertIsInstance(request, CodingRequest)
        self.assertEqual(request.workspace_dir, Path(spec["workspace_dir"]))
        self.assertEqual(request.program_id, "prog-1")

    def test_bound_task_input_wins_over_agent_args(self):
        with ExitStack() as stack:
            configured(stack)
            spec = build_coding_request(
                {"work_order_id": "WO-1", "repo_url": "https://github.com/wrong/repo", "qa_feedback": "agent copy"},
                delivery_workflow_id=None,
                bound={"repo_url": "https://github.com/example/other", "base_branch": "release", "qa_feedback": "stored"},
            )
        self.assertEqual(spec["repo_url"], "https://github.com/example/other")
        self.assertEqual(spec["base_branch"], "release")
        self.assertEqual(spec["qa_feedback"], "stored")

    def test_errors(self):
        with ExitStack() as stack:
            configured(stack, coding_backend="aider")
            with self.assertRaisesRegex(CodingRequestError, "unsupported coding_backend"):
                build_coding_request({}, delivery_workflow_id=None)
        with ExitStack() as stack:
            configured(stack)
            with self.assertRaisesRegex(CodingRequestError, "refresh requires a GitHub pr_url"):
                build_coding_request({"mode": "refresh"}, delivery_workflow_id=None)
            with self.assertRaisesRegex(CodingRequestError, "asks for implementation"):
                build_coding_request({"mode": "refresh", "pr_url": PR}, delivery_workflow_id=None, bound={"repo_url": "x"})
            stack.enter_context(patch.object(coding_job, "check_repo_allowed", return_value="repo not bound"))
            with self.assertRaises(CodingRequestError) as ctx:
                build_coding_request({"repo_url": "https://github.com/x/y"}, delivery_workflow_id=None)
            self.assertEqual(ctx.exception.extra, {"repo_url": "https://github.com/x/y"})


class RunCodingRequestTests(unittest.TestCase):
    def test_tool_and_run_share_one_path(self):
        captured: list[CodingRequest] = []

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            def implement(self, request):
                captured.append(request)
                return CodingResult(ok=True, branch="wo/wo-1", summary="done", pr_url=PR, head_sha="abc", changed_files=["a.py"])

        args = {"work_order_id": "WO-1", "work_order_body": "body", "program_id": "prog-1"}
        with ExitStack() as stack:
            configured(stack, backend=FakeBackend)
            refs = stack.enter_context(patch.object(coding_job, "record_coding_refs", return_value=[]))
            via_tool = handle_run_coding_backend(args, ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test"))
            spec = build_coding_request(args, delivery_workflow_id=None, program_id="prog-1")
            via_job = run_coding_request(spec)
        self.assertEqual(via_tool, via_job)
        self.assertEqual(captured[0], captured[1])
        self.assertEqual(refs.call_count, 2)
        self.assertEqual(via_job["pr_url"], PR)

    def test_spec_carries_backend_settings_so_workers_need_no_config(self):
        with ExitStack() as stack:
            configured(stack)
            spec = build_coding_request({"work_order_id": "WO-1", "work_order_body": "b"}, delivery_workflow_id=None, program_id="p")
        self.assertEqual(
            spec["backend"],
            {
                "binary": "opencode",
                "run_args": ["run"],
                "verification_fix_passes": 1,
                "security": {"secret_scan": True, "dependency_audit": True, "audit_severity": "high", "static_analysis": True},
            },
        )
        built: list[dict] = []

        class FakeBackend:
            def __init__(self, **kwargs):
                built.append(kwargs)

            def implement(self, request):
                return CodingResult(ok=True, branch="wo/wo-1", summary="done", pr_url=PR)

        spec["backend"]["security"]["audit_severity"] = "critical"
        with ExitStack() as stack:
            stack.enter_context(patch.object(coding_job, "load_active_runtime_config", side_effect=AssertionError("no config")))
            stack.enter_context(patch.object(coding_job, "OpenCodeBackend", FakeBackend))
            refs = stack.enter_context(patch.object(coding_job, "record_coding_refs", return_value=[]))
            stack.enter_context(patch.dict("os.environ", {"TS_HARNESS_TOOLS_ROOT": "/opt/tools"}))
            result = run_coding_request(spec, record_refs=False)
        self.assertTrue(result["ok"])
        refs.assert_not_called()
        self.assertEqual((built[0]["tools_root"], built[0]["binary_name"], built[0]["run_args"]), ("/opt/tools", "opencode", ["run"]))
        self.assertEqual(built[0]["security"].audit_severity, "critical")
        self.assertNotIn("backend", coding_request_from_spec(spec).__dict__)

    def test_tool_reports_request_errors(self):
        with ExitStack() as stack:
            configured(stack)
            result = handle_run_coding_backend(
                {"mode": "rebase"}, ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test")
            )
        self.assertEqual(result, {"ok": False, "error": "unsupported mode: rebase"})


class StageOutputTests(unittest.TestCase):
    def test_maps_the_contract_fields(self):
        output = coding_stage_output(
            {
                "ok": True,
                "work_order_id": "WO-1",
                "pr_url": PR,
                "branch": "wo/wo-1",
                "head_sha": "abc",
                "changed_files": ["a.py"],
                "summary": "done",
                "security": None,
                "log_path": "/logs/x.jsonl",
                "log_tail": "noise",
                "mode": "implement",
            }
        )
        self.assertEqual(
            output,
            {
                "work_order_id": "WO-1",
                "pr_url": PR,
                "branch": "wo/wo-1",
                "head_sha": "abc",
                "changed_files": ["a.py"],
                "summary": "done",
                "security": {},
                "log_path": "/logs/x.jsonl",
            },
        )

    def test_failed_run_carries_the_error_and_no_pr(self):
        output = coding_stage_output({"ok": False, "work_order_id": "WO-1", "error": "tests failed", "mode": "implement"})
        self.assertNotIn("pr_url", output)
        self.assertEqual(output["error"], "tests failed")

    def test_refresh_keeps_resolution_details(self):
        output = coding_stage_output(
            {"ok": True, "work_order_id": "WO-1", "pr_url": PR, "mode": "refresh", "refresh_method": "merge", "resolved_files": ["a.py"]}
        )
        self.assertEqual((output["refresh_method"], output["resolved_files"]), ("merge", ["a.py"]))


if __name__ == "__main__":
    unittest.main()
