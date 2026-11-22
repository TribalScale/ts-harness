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
    stack.enter_context(patch.object(coding_job, "check_repo_allowed_or_linked", return_value=None))
    stack.enter_context(patch.object(coding_job, "program_downgrade_note", return_value=None))
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

    def test_a_complex_work_order_starts_on_the_stronger_model(self):
        body = "# WO-3\n\n- **Complexity:** High (shared schema and two services)\n"
        with ExitStack() as stack, patch.dict("os.environ", {"TS_HARNESS_COMPLEX_CODING_MODEL": "anthropic/claude-opus-5-5"}):
            configured(stack)
            strong = build_coding_request({"work_order_id": "WO-3", "work_order_body": body}, delivery_workflow_id=None)
            plain = build_coding_request(
                {"work_order_id": "WO-4", "work_order_body": "# WO-4\n\nComplexity: Low\n"}, delivery_workflow_id=None
            )
        self.assertEqual(strong["model"], "claude-opus-5-5")
        self.assertNotEqual(plain["model"], "claude-opus-5-5")

    def test_a_program_at_its_budget_keeps_to_the_default_model(self):
        body = "# WO-3\n\n- **Complexity:** High\n"
        env = {"TS_HARNESS_COMPLEX_CODING_MODEL": "anthropic/claude-opus-5-5", "TS_HARNESS_PRE_PUSH_REVIEW_MODEL": "anthropic/claude-opus-5-5"}
        note = "Program Checkout reached its LLM budget"
        with ExitStack() as stack, patch.dict("os.environ", env):
            configured(stack)
            downgrade = stack.enter_context(patch.object(coding_job, "program_downgrade_note", return_value=note))
            complex_run = build_coding_request({"work_order_id": "WO-3", "work_order_body": body}, delivery_workflow_id=None, program_id="p-1")
            revision = build_coding_request(
                {"work_order_id": "WO-3"}, delivery_workflow_id=None, program_id="p-1", bound={"coding_model": "anthropic/claude-opus-5-5"}
            )
        downgrade.assert_called_with("p-1")
        for spec in (complex_run, revision):
            self.assertEqual((spec["model"], spec["review_model"], spec["budget_note"]), ("claude", "claude", note))
        self.assertNotIn("budget_note", coding_request_from_spec(complex_run).__dict__)

    def test_no_budget_note_when_nothing_stronger_would_run(self):
        env = {"TS_HARNESS_COMPLEX_CODING_MODEL": "off", "TS_HARNESS_PRE_PUSH_REVIEW_MODEL": "off"}
        with ExitStack() as stack, patch.dict("os.environ", env):
            configured(stack)
            downgrade = stack.enter_context(patch.object(coding_job, "program_downgrade_note", return_value="over"))
            spec = build_coding_request({"work_order_id": "WO-1"}, delivery_workflow_id=None, program_id="p-1")
        downgrade.assert_not_called()
        self.assertNotIn("budget_note", spec)
        self.assertEqual(spec["model"], "claude")

    def test_complexity_markers(self):
        for text in ("Complexity: High", "- **Complexity:** high", "| Complexity | High |", "## Complexity\n\nHigh, two services"):
            self.assertTrue(coding_job.is_complex_work_order(text), text)
        for text in ("Complexity: Medium", "the complexity of the parser is high", ""):
            self.assertFalse(coding_job.is_complex_work_order(text), text)

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
            stack.enter_context(patch.object(coding_job, "check_repo_allowed_or_linked", return_value="repo not bound"))
            with self.assertRaises(CodingRequestError) as ctx:
                build_coding_request({"repo_url": "https://github.com/x/y"}, delivery_workflow_id=None)
            self.assertEqual(ctx.exception.extra, {"repo_url": "https://github.com/x/y"})

    def test_repo_check_uses_the_workflows_tenant(self):
        from ts_harness.workflows import coding_job_stage

        with ExitStack() as stack:
            configured(stack)
            check = stack.enter_context(patch.object(coding_job, "check_repo_allowed_or_linked", return_value=None))
            build_coding_request({"repo_url": "https://github.com/example/app"}, delivery_workflow_id=None, tenant_id="acme")
            self.assertEqual(check.call_args.kwargs["tenant_id"], "acme")
            coding_job_stage.stage_spec(
                {"workflow_id": "wf-1", "tenant_id": "globex"},
                {"repo_url": "https://github.com/example/app"},
                program_id=None,
            )
            self.assertEqual(check.call_args.kwargs["tenant_id"], "globex")


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


class PrCiTests(unittest.TestCase):
    def test_pr_ci_is_read_once_and_cached(self):
        coding_job._PR_CI_CACHE.clear()
        with patch("ts_harness.workflows.repo_bootstrap._has_pr_ci", return_value=True) as has, \
                patch("ts_harness.integrations.github_client.github_for", return_value=MagicMock()):
            self.assertTrue(coding_job.repo_has_pr_ci("https://github.com/acme/api.git", "dev"))
            self.assertTrue(coding_job.repo_has_pr_ci("https://github.com/acme/api", "dev"))
        self.assertEqual(has.call_count, 1)
        coding_job._PR_CI_CACHE.clear()
        with patch("ts_harness.integrations.github_client.github_for", side_effect=RuntimeError("no token")):
            self.assertIsNone(coding_job.repo_has_pr_ci("https://github.com/acme/web", "dev"))
        coding_job._PR_CI_CACHE.clear()


class TicketComplexityTests(unittest.TestCase):
    def test_a_ticket_that_lost_the_complexity_line_gets_it_from_its_work_order(self):
        from ts_harness.jira.ticket_delivery import _with_work_order_complexity

        artifacts = {"work_orders_markdown": "# WO-001 Sync\nComplexity: High\n\n## Summary\nx\n\n# WO-002 List\nComplexity: Low\n"}
        self.assertTrue(_with_work_order_complexity("As a user...", artifacts, "WO-001").startswith("Complexity: High"))
        self.assertEqual(_with_work_order_complexity("As a user...", artifacts, "WO-002"), "As a user...")
        self.assertEqual(_with_work_order_complexity("Complexity: High\nbody", artifacts, "WO-001"), "Complexity: High\nbody")
