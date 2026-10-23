"""Unit tests for OpenCode coding backend and tool."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.implementation.auth import provider_child_env, resolve_coding_backend_credentials
from ts_harness.implementation.backends.base import CodingRequest
from ts_harness.implementation.backends.opencode import (
    OpenCodeBackend,
    build_implement_prompt,
    build_opencode_command,
)
from ts_harness.implementation.repo import work_order_branch_name
from ts_harness.implementation.test_env import TestEnvironment
from ts_harness.implementation.verification import VerifyResult
from ts_harness.implementation.work_order_context import extract_work_order_section
from ts_harness.tools.coding_backend_tool import handle_run_coding_backend
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.registry import build_default_registry


class WorkOrderContextTests(unittest.TestCase):
    def test_extract_work_order_section(self):
        md = (
            "# WO-001 Lobby\n\nImplement lobby.\n\n"
            "## Acceptance\n- a\n\n"
            "# WO-002 Matchmaking\n\nImplement matchmaking.\n"
        )
        section = extract_work_order_section(md, "WO-001")
        self.assertIn("WO-001 Lobby", section)
        self.assertIn("Implement lobby", section)
        self.assertNotIn("WO-002", section)
        self.assertIn("WO-002", extract_work_order_section(md, "WO-002"))


class OpenCodeBackendTests(unittest.TestCase):
    def test_branch_naming(self):
        self.assertEqual(work_order_branch_name("WO-001"), "wo/wo-001")
        self.assertEqual(work_order_branch_name("WO-002"), "wo/wo-002")

    def test_branch_is_unique_per_delivery_workflow(self):
        first = work_order_branch_name("WO-001", "9928a0eb-0811-4dee-bcb2-b0917d8c3026")
        second = work_order_branch_name("WO-001", "4acd196e-3005-4fa0-b947-0828ea79cbbb")
        self.assertEqual(first, "wo/wo-001-9928a0eb")
        self.assertNotEqual(first, second)

    def test_github_authenticated_url_injects_token(self):
        from ts_harness.implementation.repo import github_authenticated_url

        url = github_authenticated_url(
            "https://github.com/org/app.git",
            token="ghp_test",
        )
        self.assertEqual(url, "https://x-access-token:ghp_test@github.com/org/app.git")
        self.assertEqual(
            github_authenticated_url("https://github.com/org/app.git", token=""),
            "https://github.com/org/app.git",
        )

    def test_build_command_includes_model(self):
        cmd = build_opencode_command(
            binary="/tools/opencode",
            repo_dir=Path("/repo"),
            prompt="do it",
            run_args=["run", "--auto"],
            provider="anthropic",
            model="claude-sonnet-4",
        )
        self.assertEqual(cmd[0], "/tools/opencode")
        self.assertIn("--dir", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("anthropic/claude-sonnet-4", cmd)
        self.assertEqual(cmd[-1], "do it")

    def test_implement_prompt_requires_the_repos_own_test_stack(self):
        from ts_harness.implementation.test_env import HOST_TOOLCHAINS

        prompt = build_implement_prompt("WO-1", "# WO-1\n\nAdd the endpoint")
        self.assertIn("Use the repo's existing language and test runner", prompt)
        self.assertIn("Never add a module or script that stands in for a test framework", prompt)
        for toolchain in HOST_TOOLCHAINS:
            self.assertIn(toolchain, prompt)

    def test_provider_child_env_overrides_anthropic(self):
        env = provider_child_env("anthropic", "coding-key")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "coding-key")
        self.assertEqual(env["CODING_BACKEND_API_KEY"], "coding-key")

    def test_resolve_credentials_from_auth_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth-profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "CODING_BACKEND_API_KEY": "from-profile",
                        "CODING_BACKEND_PROVIDER": "openai",
                    }
                )
            )
            provider, key = resolve_coding_backend_credentials(auth_profiles_path=path)
            self.assertEqual(provider, "openai")
            self.assertEqual(key, "from-profile")

    def test_implement_builds_command_and_opens_pr(self):
        calls: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools" / "opencode" / "current" / "bin"
            tools.mkdir(parents=True)
            binary = tools / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

            workspace = Path(tmp) / "ws"
            status_calls = {"n": 0}

            def runner_with_clone(cmd, **kwargs):
                calls.append({"cmd": cmd, "kwargs": kwargs})
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                if cmd[:2] == ["git", "clone"]:
                    self.assertIn("x-access-token:gh-token@", cmd[-2])
                    repo = Path(cmd[-1])
                    (repo / ".git").mkdir(parents=True)
                elif cmd[:1] == ["gh"] and "pr" in cmd and "create" in cmd:
                    result.stdout = "https://github.com/example/app/pull/42\n"
                elif cmd[0] == "git" and "status" in cmd and "--porcelain" in cmd:
                    status_calls["n"] += 1
                    # First statuses (list_changed / pre-commit): dirty. After commit: clean.
                    if status_calls["n"] <= 2:
                        result.stdout = " M src/app.py\n"
                return result

            backend = OpenCodeBackend(
                tools_root=str(Path(tmp) / "tools"),
                runner=runner_with_clone,
            )
            os.environ["CODING_BACKEND_API_KEY"] = "coding-secret"
            os.environ["CODING_BACKEND_PROVIDER"] = "anthropic"
            os.environ["ANTHROPIC_API_KEY"] = "gateway-secret"
            os.environ["GITHUB_TOKEN"] = "gh-token"
            try:
                with patch(
                    "ts_harness.implementation.backends.opencode.verify_implementation",
                    return_value=VerifyResult(
                        ok=True,
                        reasons=[],
                        summary="verification passed",
                        log_tail="",
                    ),
                ):
                    result = backend.implement(
                        CodingRequest(
                            repo_url="https://github.com/example/app.git",
                            base_branch="main",
                            work_order_id="WO-001",
                            work_order_body="# WO-001\n\nDo the thing.",
                            workspace_dir=workspace,
                            provider="anthropic",
                            model="claude-sonnet-4",
                        )
                    )
            finally:
                os.environ.pop("CODING_BACKEND_API_KEY", None)
                os.environ.pop("CODING_BACKEND_PROVIDER", None)
                os.environ.pop("GITHUB_TOKEN", None)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.pr_url, "https://github.com/example/app/pull/42")
            opencode_calls = [c for c in calls if str(c["cmd"][0]).endswith("/opencode")]
            self.assertEqual(len(opencode_calls), 1)
            env = opencode_calls[0]["kwargs"]["env"]
            self.assertEqual(env["ANTHROPIC_API_KEY"], "coding-secret")
            self.assertIn("anthropic/claude-sonnet-4", opencode_calls[0]["cmd"])
            self.assertTrue(any(c["cmd"][:1] == ["gh"] for c in calls))

    def test_implement_runs_verification_fix_pass_before_pr(self):
        calls: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools" / "opencode" / "current" / "bin"
            tools.mkdir(parents=True)
            binary = tools / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

            workspace = Path(tmp) / "ws"

            def runner_with_clone(cmd, **kwargs):
                calls.append({"cmd": cmd, "kwargs": kwargs})
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                if cmd[:2] == ["git", "clone"]:
                    repo = Path(cmd[-1])
                    (repo / ".git").mkdir(parents=True)
                elif cmd[:1] == ["gh"] and "pr" in cmd and "create" in cmd:
                    result.stdout = "https://github.com/example/app/pull/42\n"
                elif cmd[0] == "git" and "status" in cmd and "--porcelain" in cmd:
                    result.stdout = " M src/app.py\n"
                return result

            failed = VerifyResult(
                ok=False,
                reasons=["no_test_runner"],
                summary="verification failed: no_test_runner",
                log_tail="no test command detected",
            )
            passed = VerifyResult(
                ok=True,
                reasons=[],
                summary="verification passed",
                log_tail="pytest passed",
            )

            backend = OpenCodeBackend(
                tools_root=str(Path(tmp) / "tools"),
                runner=runner_with_clone,
            )
            os.environ["CODING_BACKEND_API_KEY"] = "coding-secret"
            os.environ["CODING_BACKEND_PROVIDER"] = "anthropic"
            os.environ["GITHUB_TOKEN"] = "gh-token"
            try:
                with patch(
                    "ts_harness.implementation.backends.opencode.verify_implementation",
                    side_effect=[failed, passed],
                ) as verify:
                    result = backend.implement(
                        CodingRequest(
                            repo_url="https://github.com/example/app.git",
                            base_branch="main",
                            work_order_id="WO-001",
                            work_order_body="# WO-001\n\nGiven a runner when code changes then tests pass.",
                            workspace_dir=workspace,
                            provider="anthropic",
                            model="claude-sonnet-4",
                        )
                    )
            finally:
                os.environ.pop("CODING_BACKEND_API_KEY", None)
                os.environ.pop("CODING_BACKEND_PROVIDER", None)
                os.environ.pop("GITHUB_TOKEN", None)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.pr_url, "https://github.com/example/app/pull/42")
            self.assertEqual(verify.call_count, 2)
            opencode_calls = [c for c in calls if str(c["cmd"][0]).endswith("/opencode")]
            self.assertEqual(len(opencode_calls), 2)
            self.assertIn("Host verification failed", opencode_calls[1]["cmd"][-1])
            self.assertIn("no_test_runner", opencode_calls[1]["cmd"][-1])
            self.assertTrue(any(c["cmd"][:3] == ["gh", "pr", "create"] for c in calls))

    def test_implement_stops_before_pr_when_verification_fix_passes_exhausted(self):
        calls: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools" / "opencode" / "current" / "bin"
            tools.mkdir(parents=True)
            binary = tools / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

            workspace = Path(tmp) / "ws"

            def runner_with_clone(cmd, **kwargs):
                calls.append({"cmd": cmd, "kwargs": kwargs})
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                if cmd[:2] == ["git", "clone"]:
                    repo = Path(cmd[-1])
                    (repo / ".git").mkdir(parents=True)
                elif cmd[0] == "git" and "status" in cmd and "--porcelain" in cmd:
                    result.stdout = " M src/app.py\n"
                return result

            failed = VerifyResult(
                ok=False,
                reasons=["no_test_runner"],
                summary="verification failed: no_test_runner",
                log_tail="no test command detected",
            )

            backend = OpenCodeBackend(
                tools_root=str(Path(tmp) / "tools"),
                runner=runner_with_clone,
                verification_fix_passes=1,
            )
            os.environ["CODING_BACKEND_API_KEY"] = "coding-secret"
            os.environ["CODING_BACKEND_PROVIDER"] = "anthropic"
            os.environ["GITHUB_TOKEN"] = "gh-token"
            try:
                with patch(
                    "ts_harness.implementation.backends.opencode.verify_implementation",
                    side_effect=[failed, failed],
                ):
                    result = backend.implement(
                        CodingRequest(
                            repo_url="https://github.com/example/app.git",
                            base_branch="main",
                            work_order_id="WO-001",
                            work_order_body="# WO-001\n\nGiven a runner when code changes then tests pass.",
                            workspace_dir=workspace,
                            provider="anthropic",
                            model="claude-sonnet-4",
                        )
                    )
            finally:
                os.environ.pop("CODING_BACKEND_API_KEY", None)
                os.environ.pop("CODING_BACKEND_PROVIDER", None)
                os.environ.pop("GITHUB_TOKEN", None)

            self.assertFalse(result.ok)
            self.assertIn("verification", result.error or "")
            self.assertIn("fix_passes_used=1", result.summary)
            self.assertIn("fix_passes_used=1", result.error or "")
            opencode_calls = [c for c in calls if str(c["cmd"][0]).endswith("/opencode")]
            self.assertEqual(len(opencode_calls), 2)
            self.assertFalse(any(c["cmd"][:3] == ["gh", "pr", "create"] for c in calls))

    def test_implement_allows_chore_when_verification_skips_missing_runner(self):
        calls: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools" / "opencode" / "current" / "bin"
            tools.mkdir(parents=True)
            binary = tools / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

            workspace = Path(tmp) / "ws"

            def runner_with_clone(cmd, **kwargs):
                calls.append({"cmd": cmd, "kwargs": kwargs})
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                if cmd[:2] == ["git", "clone"]:
                    repo = Path(cmd[-1])
                    (repo / ".git").mkdir(parents=True)
                elif cmd[:1] == ["gh"] and "pr" in cmd and "create" in cmd:
                    result.stdout = "https://github.com/example/app/pull/43\n"
                elif cmd[0] == "git" and "status" in cmd and "--porcelain" in cmd:
                    result.stdout = " M README.md\n"
                return result

            backend = OpenCodeBackend(
                tools_root=str(Path(tmp) / "tools"),
                runner=runner_with_clone,
            )
            os.environ["CODING_BACKEND_API_KEY"] = "coding-secret"
            os.environ["CODING_BACKEND_PROVIDER"] = "anthropic"
            os.environ["GITHUB_TOKEN"] = "gh-token"
            try:
                result = backend.implement(
                    CodingRequest(
                        repo_url="https://github.com/example/app.git",
                        base_branch="main",
                        work_order_id="WO-002",
                        work_order_body="# WO-002\n\nChore: rename only.",
                        workspace_dir=workspace,
                        provider="anthropic",
                        model="claude-sonnet-4",
                    )
                )
            finally:
                os.environ.pop("CODING_BACKEND_API_KEY", None)
                os.environ.pop("CODING_BACKEND_PROVIDER", None)
                os.environ.pop("GITHUB_TOKEN", None)

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.pr_url, "https://github.com/example/app/pull/43")
            opencode_calls = [c for c in calls if str(c["cmd"][0]).endswith("/opencode")]
            self.assertEqual(len(opencode_calls), 1)

    def test_path_prompt_when_docs_materialized(self):
        prompt = build_implement_prompt(
            "WO-001",
            "# WO-001\n\nBody",
            primary_spec_path="docs/planner/work-orders/WO-001-lobby.md",
            docs_materialized=True,
            skills_materialized=True,
        )
        self.assertIn("Primary spec: docs/planner/work-orders/WO-001-lobby.md", prompt)
        self.assertIn("docs/refinery/", prompt)
        self.assertIn("implement-work-order", prompt)
        self.assertIn("coding-practices", prompt)
        self.assertIn("bdd-acceptance", prompt)
        self.assertIn("brainstorming", prompt.lower())
        self.assertIn("implement immediately", prompt.lower())
        self.assertNotIn("Body", prompt)

    def test_qa_feedback_prompt_asks_to_fix_existing_pr(self):
        prompt = build_implement_prompt(
            "WO-003",
            "# WO-003\n\nBody",
            qa_feedback="No evidence of implementation or testing",
            pr_url="https://github.com/org/app/pull/8",
        )
        self.assertIn("QA rejected", prompt)
        self.assertIn("No evidence of implementation or testing", prompt)
        self.assertIn("https://github.com/org/app/pull/8", prompt)
        self.assertIn("existing pull request", prompt.lower())

    def test_review_feedback_prompt_lists_findings_before_qa_feedback(self):
        prompt = build_implement_prompt(
            "WO-003",
            "# WO-003\n\nBody",
            review_feedback="- [major/correctness] a.py:3 off by one",
            qa_feedback="Missing evidence",
            pr_url="https://github.com/org/app/pull/8",
        )
        self.assertIn("A review blocked this delivery", prompt)
        self.assertIn("a.py:3 off by one", prompt)
        self.assertLess(prompt.index("A review blocked"), prompt.index("QA rejected"))
        self.assertEqual(prompt.count("https://github.com/org/app/pull/8"), 1)

    def test_prompt_keeps_earlier_human_requests(self):
        prompt = build_implement_prompt(
            "WO-903",
            "# WO-903\n\nBody",
            review_feedback="- [major/correctness] slugkit/__init__.py:7 remove normalization",
            human_change_requests=["Human reviewer alice requested changes:\nConvert accents to ASCII."],
        )
        self.assertIn("A human reviewer already requested these changes", prompt)
        self.assertIn("- Human reviewer alice requested changes:\nConvert accents to ASCII.", prompt)
        self.assertNotIn("already requested", build_implement_prompt("WO-903", "# WO-903", human_change_requests=[" "]))

    def test_implement_materializes_planning_docs_before_opencode(self):
        calls: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp) / "tools" / "opencode" / "current" / "bin"
            tools.mkdir(parents=True)
            binary = tools / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            workspace = Path(tmp) / "ws"
            status_calls = {"n": 0}
            repo_holder: dict[str, Path] = {}

            def runner_with_clone(cmd, **kwargs):
                calls.append({"cmd": cmd, "kwargs": kwargs})
                result = MagicMock()
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                if cmd[:2] == ["git", "clone"]:
                    repo = Path(cmd[-1])
                    (repo / ".git").mkdir(parents=True)
                    repo_holder["repo"] = repo
                elif cmd[:1] == ["gh"] and "pr" in cmd and "create" in cmd:
                    result.stdout = "https://github.com/example/app/pull/9\n"
                    body_idx = cmd.index("--body") + 1 if "--body" in cmd else -1
                    if body_idx > 0:
                        self.assertIn("Planning docs", cmd[body_idx])
                elif cmd[0] == "git" and "status" in cmd and "--porcelain" in cmd:
                    status_calls["n"] += 1
                    if status_calls["n"] <= 2:
                        result.stdout = " M docs/planner/work-orders/WO-001-lobby.md\n"
                elif str(cmd[0]).endswith("/opencode"):
                    repo = repo_holder["repo"]
                    frd_files = list((repo / "docs/refinery/features").glob("FRD-001*.md"))
                    wo_files = list((repo / "docs/planner/work-orders").glob("WO-001*.md"))
                    self.assertTrue(frd_files, "FRD file missing before OpenCode")
                    self.assertTrue(wo_files, "WO file missing before OpenCode")
                    prompt = cmd[-1]
                    self.assertIn("Primary spec:", prompt)
                    self.assertIn("docs/planner/work-orders/", prompt)
                return result

            backend = OpenCodeBackend(
                tools_root=str(Path(tmp) / "tools"),
                runner=runner_with_clone,
            )
            os.environ["CODING_BACKEND_API_KEY"] = "coding-secret"
            os.environ["CODING_BACKEND_PROVIDER"] = "anthropic"
            os.environ["GITHUB_TOKEN"] = "gh-token"
            try:
                with patch(
                    "ts_harness.implementation.backends.opencode.verify_implementation",
                    return_value=VerifyResult(
                        ok=True,
                        reasons=[],
                        summary="verification passed",
                        log_tail="",
                    ),
                ) as verify_mock:
                    result = backend.implement(
                        CodingRequest(
                            repo_url="https://github.com/example/app.git",
                            base_branch="main",
                            work_order_id="WO-001",
                            work_order_body="## WO-001 Lobby\n\nBuild lobby.\n",
                            workspace_dir=workspace,
                            provider="anthropic",
                            model="claude-sonnet-4",
                            refinery_artifacts={
                                "frds_markdown": "## FRD-001 Matchmaking\n\nQueue.\n",
                                "product_overview_markdown": "# Overview\n",
                                "requirements_markdown": "# Req\n",
                                "feature_tree_markdown": "# Tree\n",
                            },
                            foundry_artifacts={
                                "blueprints_markdown": "## COMP-001 Server\n\nState.\n",
                                "architecture_decisions_markdown": "## ADR-001\n\nWS.\n",
                                "architecture_summary_markdown": "# Summary\n",
                            },
                            planner_artifacts={
                                "work_orders_markdown": "## WO-001 Lobby\n\nBuild lobby.\n",
                                "context_graph_markdown": "# Graph\n",
                                "execution_phases_markdown": "# Phases\n",
                            },
                        )
                    )
            finally:
                os.environ.pop("CODING_BACKEND_API_KEY", None)
                os.environ.pop("CODING_BACKEND_PROVIDER", None)
                os.environ.pop("GITHUB_TOKEN", None)

            self.assertTrue(result.ok, result.error)
            self.assertTrue(any(str(c["cmd"][0]).endswith("/opencode") for c in calls))
            venv = workspace / ".ts-harness" / "venv"
            commands = [c["cmd"] for c in calls]
            venv_at = commands.index(["python3", "-m", "venv", str(venv)])
            opencode_at = next(i for i, c in enumerate(commands) if str(c[0]).endswith("/opencode"))
            self.assertLess(venv_at, opencode_at)
            opencode_env = calls[opencode_at]["kwargs"]["env"]
            self.assertEqual(opencode_env["VIRTUAL_ENV"], str(venv))
            self.assertEqual(opencode_env["CODING_BACKEND_API_KEY"], "coding-secret")
            self.assertEqual(verify_mock.call_args.kwargs["test_env"].python, str(venv / "bin" / "python"))

    def test_first_delivery_reverts_undeclared_files_before_verification(self):
        commands: list[list[str]] = []

        def runner(args, **_kwargs):
            commands.append(list(args))
            completed = MagicMock()
            if args[:3] == ["git", "diff", "--name-only"] and "--cached" not in args:
                completed.stdout = "app/pricing.py\nrequirements-dev.txt\n"
            else:
                completed.stdout = ""
            completed.returncode = 0
            return completed

        backend = OpenCodeBackend(tools_root="/nope", runner=runner)
        request = CodingRequest(
            repo_url="https://github.com/example/app.git",
            base_branch="main",
            work_order_id="WO-301",
            work_order_body="# WO-301\n\n## Files\n- app/pricing.py\n",
            workspace_dir=Path("/tmp/ws"),
            provider="openai",
        )
        backend._revert_undeclared(Path("/tmp/repo"), request)
        reverted = [cmd for cmd in commands if cmd[:2] == ["git", "checkout"]]
        self.assertEqual(reverted, [["git", "checkout", "HEAD", "--", "requirements-dev.txt"]])

    def test_review_revision_does_not_revert_before_verification(self):
        commands: list[list[str]] = []

        def runner(args, **_kwargs):
            commands.append(list(args))
            completed = MagicMock()
            completed.stdout = "requirements-dev.txt\n"
            completed.returncode = 0
            return completed

        backend = OpenCodeBackend(tools_root="/nope", runner=runner)
        request = CodingRequest(
            repo_url="https://github.com/example/app.git",
            base_branch="main",
            work_order_id="WO-301",
            work_order_body="# WO-301\n\n## Files\n- app/pricing.py\n",
            workspace_dir=Path("/tmp/ws"),
            provider="openai",
            revision_files=["app/pricing.py"],
        )
        backend._revert_undeclared(Path("/tmp/repo"), request)
        self.assertEqual(commands, [])

    def test_missing_provider_fails(self):
        backend = OpenCodeBackend(tools_root="/nope", runner=MagicMock())
        os.environ.pop("CODING_BACKEND_PROVIDER", None)
        os.environ.pop("CODING_BACKEND_API_KEY", None)
        os.environ["GITHUB_TOKEN"] = "gh-token"
        try:
            result = backend.implement(
                CodingRequest(
                    repo_url="https://example/repo.git",
                    base_branch="main",
                    work_order_id="WO-001",
                    work_order_body="body",
                    workspace_dir=Path("/tmp/ws"),
                    provider="",
                )
            )
        finally:
            os.environ.pop("GITHUB_TOKEN", None)
        self.assertFalse(result.ok)
        self.assertIn("PROVIDER", result.error or "")

    def test_missing_github_token_fails(self):
        backend = OpenCodeBackend(tools_root="/nope", runner=MagicMock())
        os.environ["CODING_BACKEND_PROVIDER"] = "anthropic"
        os.environ["CODING_BACKEND_API_KEY"] = "key"
        os.environ.pop("GITHUB_TOKEN", None)
        try:
            result = backend.implement(
                CodingRequest(
                    repo_url="https://github.com/example/app.git",
                    base_branch="main",
                    work_order_id="WO-001",
                    work_order_body="body",
                    workspace_dir=Path("/tmp/ws"),
                    provider="anthropic",
                )
            )
        finally:
            os.environ.pop("CODING_BACKEND_PROVIDER", None)
            os.environ.pop("CODING_BACKEND_API_KEY", None)
        self.assertFalse(result.ok)
        self.assertIn("GITHUB_TOKEN", result.error or "")


PR = "https://github.com/example/app/pull/7"


def _pull(sha, **extra):
    return {"state": "open", "merged": False, "head": {"sha": sha, "ref": "wo/wo-001"}, "base": {"ref": "main"}, **extra}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        tools = root / "tools" / "opencode" / "current" / "bin"
        tools.mkdir(parents=True)
        (tools / "opencode").write_text("#!/bin/sh\nexit 0\n")
        (tools / "opencode").chmod(0o755)
        self.tools_root = str(root / "tools")
        self.workspace = root / "ws"
        self.calls: list[list[str]] = []
        self.merge_rc = 0
        self.conflicts = ""
        self.on_opencode = None
        env = {
            "CODING_BACKEND_API_KEY": "k",
            "CODING_BACKEND_PROVIDER": "anthropic",
            "GITHUB_TOKEN": "gh",
        }
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.verify = patch(
            "ts_harness.implementation.backends.opencode.verify_implementation",
            return_value=VerifyResult(ok=True, reasons=[], summary="verification passed", log_tail=""),
        )
        self.verify_mock = self.verify.start()

    def tearDown(self):
        self.verify.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _runner(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        result = MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "clone"]:
            (Path(cmd[-1]) / ".git").mkdir(parents=True)
        elif "merge" in cmd and cmd[0] == "git":
            result.returncode = self.merge_rc
        elif "--diff-filter=U" in cmd:
            result.stdout = self.conflicts
        elif "rev-parse" in cmd:
            result.stdout = "merged-sha\n"
        elif str(cmd[0]).endswith("/opencode") and self.on_opencode:
            self.on_opencode()
        return result

    def _backend(self, github):
        return OpenCodeBackend(tools_root=self.tools_root, runner=self._runner, github=github, sleep=lambda _s: None)

    def _request(self):
        return CodingRequest(
            repo_url="https://github.com/example/app.git",
            base_branch="main",
            work_order_id="WO-001",
            work_order_body="# WO-001\n\nDo the thing.",
            workspace_dir=self.workspace,
            provider="anthropic",
            pr_url=PR,
            mode="refresh",
        )

    def _conflict_github(self):
        from ts_harness.integrations.github_client import GitHubError

        github = MagicMock()
        github.get_pull.return_value = _pull("old")
        github.update_branch.side_effect = GitHubError(422, "merge conflict between base and head")
        return github

    def test_server_merge_needs_no_clone(self):
        github = MagicMock()
        github.get_pull.side_effect = [_pull("old"), _pull("old"), _pull("new")]
        result = self._backend(github).implement(self._request())
        self.assertTrue(result.ok, result.error)
        self.assertEqual((result.refresh_method, result.head_sha), ("server_merge", "new"))
        github.update_branch.assert_called_once_with("example", "app", 7, expected_head_sha="old")
        self.assertEqual(self.calls, [])

    def test_moved_head_is_retried_once(self):
        from ts_harness.integrations.github_client import GitHubError

        github = MagicMock()
        github.get_pull.side_effect = [_pull("old"), _pull("moved"), _pull("new")]
        github.update_branch.side_effect = [GitHubError(422, "expected_head_sha does not match"), {}]
        result = self._backend(github).implement(self._request())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(github.update_branch.call_args_list[1].kwargs["expected_head_sha"], "moved")

    def test_conflict_is_resolved_locally_and_pushed(self):
        self.merge_rc = 1
        self.conflicts = "src/app.py\n"
        result = self._backend(self._conflict_github()).implement(self._request())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.refresh_method, "conflict_resolution")
        self.assertEqual(result.resolved_files, ["src/app.py"])
        self.assertEqual(result.head_sha, "merged-sha")
        opencode = [c for c in self.calls if str(c[0]).endswith("/opencode")]
        self.assertEqual(len(opencode), 1)
        self.assertIn("Resolve the merge conflicts only", opencode[0][-1])
        self.assertIn("- src/app.py", opencode[0][-1])
        self.assertTrue(any(c[3:5] == ["checkout", "-B"] for c in self.calls if c[0] == "git"))
        self.assertTrue(any("push" in c for c in self.calls))
        self.assertIsNone(self.verify_mock.call_args.kwargs["changed_files"])

    def test_local_merge_prepares_test_env_for_opencode_and_verify(self):
        self.merge_rc = 1
        self.conflicts = "src/app.py\n"
        seen_env: list[dict] = []
        original = self._runner

        def runner(cmd, **kwargs):
            if str(cmd[0]).endswith("/opencode"):
                seen_env.append(kwargs["env"])
            return original(cmd, **kwargs)

        backend = self._backend(self._conflict_github())
        backend.runner = runner
        result = backend.implement(self._request())
        self.assertTrue(result.ok, result.error)
        venv = self.workspace / ".ts-harness" / "venv"
        self.assertIn(["python3", "-m", "venv", str(venv)], self.calls)
        self.assertEqual(seen_env[0]["VIRTUAL_ENV"], str(venv))
        self.assertEqual(self.verify_mock.call_args.kwargs["test_env"].env["VIRTUAL_ENV"], str(venv))

    def test_clean_local_merge_skips_opencode(self):
        result = self._backend(self._conflict_github()).implement(self._request())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.refresh_method, "local_merge")
        self.assertFalse(any(str(c[0]).endswith("/opencode") for c in self.calls))

    def test_remaining_markers_fail(self):
        self.merge_rc = 1
        self.conflicts = "src/app.py\n"

        def leave_markers():
            path = self.workspace / "repo" / "src" / "app.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> origin/main\n")

        self.on_opencode = leave_markers
        result = self._backend(self._conflict_github()).implement(self._request())
        self.assertFalse(result.ok)
        self.assertIn("conflict_markers_remain: src/app.py", result.error)
        self.assertFalse(any("push" in c for c in self.calls))

    def test_merged_pr_is_skipped(self):
        github = MagicMock()
        github.get_pull.return_value = _pull("old", merged=True, state="closed")
        result = self._backend(github).implement(self._request())
        self.assertTrue(result.ok)
        self.assertEqual(result.refresh_method, "skipped")
        github.update_branch.assert_not_called()


class CodingBackendToolTests(unittest.TestCase):
    def test_tool_registered_in_coding_backend_toolset(self):
        registry = build_default_registry()
        tool = registry.snapshot().get("run_coding_backend")
        self.assertIsNotNone(tool)
        self.assertIn("coding-backend", tool.toolsets)
        self.assertIn("refinery_artifacts", tool.parameters["properties"])

    def test_tool_denies_non_dev_agent(self):
        ctx = ToolPolicyContext(tenant_id="default", agent_id="pm-agent", source="test")
        result = handle_run_coding_backend(
            {"work_order_id": "WO-001", "work_order_body": "x"},
            ctx,
        )
        self.assertFalse(result["ok"])

    def test_tool_forwards_planning_packs_to_coding_request(self):
        from unittest.mock import patch

        from ts_harness.implementation.backends.base import CodingResult

        captured: list[CodingRequest] = []

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            def implement(self, request: CodingRequest) -> CodingResult:
                captured.append(request)
                return CodingResult(
                    ok=True,
                    branch="wo/wo-001",
                    summary="ok",
                    pr_url="https://github.com/example/app/pull/1",
                )

        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test")
        packs = {
            "refinery_artifacts": {"frds_markdown": "## FRD-001"},
            "foundry_artifacts": {"blueprints_markdown": "## COMP-001"},
            "planner_artifacts": {"work_orders_markdown": "## WO-001"},
        }
        with (
            patch(
                "ts_harness.implementation.coding_job.load_active_runtime_config",
                return_value=MagicMock(),
            ),
            patch(
                "ts_harness.implementation.coding_job.implementation_config",
                return_value=MagicMock(coding_backend="opencode", tools_root="/tools"),
            ),
            patch(
                "ts_harness.implementation.coding_job.repo_for_workflow",
                return_value=MagicMock(
                    url="https://github.com/example/app.git",
                    base_branch="dev",
                    clone_depth=1,
                ),
            ),
            patch(
                "ts_harness.implementation.coding_job.opencode_backend_config",
                return_value=MagicMock(provider="anthropic", model="m", binary="opencode", run_args=["run"]),
            ),
            patch(
                "ts_harness.implementation.coding_job.OpenCodeBackend",
                FakeBackend,
            ),
        ):
            result = handle_run_coding_backend(
                {
                    "work_order_id": "WO-001",
                    "work_order_body": "# WO-001\n",
                    **packs,
                    "approved_prd": {"prd_markdown": "# PRD"},
                },
                ctx,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].refinery_artifacts, packs["refinery_artifacts"])
        self.assertEqual(captured[0].foundry_artifacts, packs["foundry_artifacts"])
        self.assertEqual(captured[0].planner_artifacts, packs["planner_artifacts"])
        self.assertEqual(captured[0].approved_prd_markdown, "# PRD")

    def test_tool_forwards_qa_feedback_to_coding_request(self):
        from unittest.mock import patch

        from ts_harness.implementation.backends.base import CodingResult

        captured: list[CodingRequest] = []

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            def implement(self, request: CodingRequest) -> CodingResult:
                captured.append(request)
                return CodingResult(
                    ok=True,
                    branch="wo/wo-003",
                    summary="ok",
                    pr_url="https://github.com/org/app/pull/8",
                )

        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test")
        with (
            patch(
                "ts_harness.implementation.coding_job.load_active_runtime_config",
                return_value=MagicMock(),
            ),
            patch(
                "ts_harness.implementation.coding_job.implementation_config",
                return_value=MagicMock(coding_backend="opencode", tools_root="/tools"),
            ),
            patch(
                "ts_harness.implementation.coding_job.repo_for_workflow",
                return_value=MagicMock(
                    url="https://github.com/example/app.git",
                    base_branch="dev",
                    clone_depth=1,
                ),
            ),
            patch(
                "ts_harness.implementation.coding_job.opencode_backend_config",
                return_value=MagicMock(
                    provider="anthropic",
                    model="m",
                    binary="opencode",
                    run_args=["run"],
                ),
            ),
            patch(
                "ts_harness.implementation.coding_job.OpenCodeBackend",
                FakeBackend,
            ),
        ):
            result = handle_run_coding_backend(
                {
                    "work_order_id": "WO-003",
                    "work_order_body": "# WO-003\n",
                    "qa_feedback": "No evidence of implementation",
                    "pr_url": "https://github.com/org/app/pull/8",
                },
                ctx,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(captured[0].qa_feedback, "No evidence of implementation")
        self.assertEqual(captured[0].pr_url, "https://github.com/org/app/pull/8")
        self.assertEqual(captured[0].mode, "implement")

    def _run_tool(self, args, backend_result):
        captured: list[CodingRequest] = []

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            def implement(self, request):
                captured.append(request)
                return backend_result

        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test")
        repo = MagicMock(url="https://github.com/example/app.git", base_branch="dev", clone_depth=1)
        backend_cfg = MagicMock(provider="anthropic", model="m", binary="opencode", run_args=["run"])
        with (
            patch("ts_harness.implementation.coding_job.load_active_runtime_config", return_value=MagicMock()),
            patch(
                "ts_harness.implementation.coding_job.implementation_config",
                return_value=MagicMock(coding_backend="opencode", tools_root="/tools"),
            ),
            patch("ts_harness.implementation.coding_job.repo_for_workflow", return_value=repo),
            patch("ts_harness.implementation.coding_job.opencode_backend_config", return_value=backend_cfg),
            patch("ts_harness.implementation.coding_job.OpenCodeBackend", FakeBackend),
        ):
            return handle_run_coding_backend(args, ctx), captured

    def test_tool_passes_refresh_mode_and_returns_refresh_fields(self):
        from ts_harness.implementation.backends.base import CodingResult

        result, captured = self._run_tool(
            {
                "work_order_id": "WO-003",
                "work_order_body": "# WO-003\n",
                "mode": "refresh",
                "pr_url": "https://github.com/org/app/pull/8",
                "review_feedback": "fix naming",
            },
            CodingResult(ok=True, branch="wo/wo-003", summary="ok", head_sha="abc", refresh_method="server_merge"),
        )
        self.assertEqual((captured[0].mode, captured[0].review_feedback), ("refresh", "fix naming"))
        self.assertEqual(captured[0].repo_url, "https://github.com/org/app.git")
        self.assertEqual((result["head_sha"], result["refresh_method"], result["mode"]), ("abc", "server_merge", "refresh"))

    def test_tool_keeps_the_work_order_repo_when_the_agent_names_another(self):
        from ts_harness.implementation.backends.base import CodingResult

        captured: list[CodingRequest] = []

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            def implement(self, request):
                captured.append(request)
                return CodingResult(ok=True, branch="wo/wo-306", summary="ok")

        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="dev-agent",
            source="test",
            linked_workflow_id="wf-306",
        )
        repo = MagicMock(url="https://github.com/example-org/prd-dev-project.git", base_branch="dev", clone_depth=1)
        with (
            patch("ts_harness.implementation.coding_job.load_active_runtime_config", return_value=MagicMock()),
            patch(
                "ts_harness.implementation.coding_job.implementation_config",
                return_value=MagicMock(coding_backend="opencode", tools_root="/tools"),
            ),
            patch("ts_harness.implementation.coding_job.repo_for_workflow", return_value=repo),
            patch(
                "ts_harness.implementation.coding_job.opencode_backend_config",
                return_value=MagicMock(provider="anthropic", model="m", binary="opencode", run_args=["run"]),
            ),
            patch("ts_harness.implementation.coding_job.OpenCodeBackend", FakeBackend),
            patch("ts_harness.tools.coding_backend_tool.delivery_binding", return_value={
                "repo_url": "https://github.com/example-org/test-ts-harness-repo",
                "base_branch": "main",
                "pr_url": "https://github.com/example-org/test-ts-harness-repo/pull/19",
            }),
        ):
            result = handle_run_coding_backend(
                {
                    "work_order_id": "WO-306",
                    "work_order_body": "# WO-306\n",
                    "repo_url": "https://github.com/example-org/prd-dev-project",
                    "base_branch": "dev",
                },
                ctx,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(captured[0].repo_url, "https://github.com/example-org/test-ts-harness-repo")
        self.assertEqual(captured[0].base_branch, "main")
        self.assertEqual(captured[0].pr_url, "https://github.com/example-org/test-ts-harness-repo/pull/19")
        self.assertEqual(captured[0].delivery_workflow_id, "wf-306")
        self.assertEqual(captured[0].workspace_dir, Path("/workspace/dev-agent/coding-backend/WO-306-wf-306"))

    def _run_tool_bound(self, args, binding):
        from unittest.mock import patch

        from ts_harness.implementation.backends.base import CodingResult

        captured: list[CodingRequest] = []

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            def implement(self, request: CodingRequest) -> CodingResult:
                captured.append(request)
                return CodingResult(ok=True, branch="wo/wo-903", summary="ok")

        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test", linked_workflow_id="wf-903")
        repo = MagicMock(url="https://github.com/org/app.git", base_branch="dev", clone_depth=1)
        with (
            patch("ts_harness.implementation.coding_job.load_active_runtime_config", return_value=MagicMock()),
            patch(
                "ts_harness.implementation.coding_job.implementation_config",
                return_value=MagicMock(coding_backend="opencode", tools_root="/tools"),
            ),
            patch("ts_harness.implementation.coding_job.repo_for_workflow", return_value=repo),
            patch(
                "ts_harness.implementation.coding_job.opencode_backend_config",
                return_value=MagicMock(provider="anthropic", model="m", binary="opencode", run_args=["run"]),
            ),
            patch("ts_harness.implementation.coding_job.OpenCodeBackend", FakeBackend),
            patch("ts_harness.tools.coding_backend_tool.delivery_binding", return_value=binding),
        ):
            handle_run_coding_backend({"work_order_id": "WO-903", **args}, ctx)
        return captured[0]

    def test_tool_uses_the_stored_feedback_over_the_agents_retyped_copy(self):
        request = self._run_tool_bound(
            {"work_order_body": "# WO-903 Caf\x00e9", "review_feedback": "'Caf\x00e9' must stay", "qa_feedback": "invented"},
            {
                "repo_url": "https://github.com/org/app",
                "work_order_body": "# WO-903 Café",
                "review_feedback": "'Café' must stay",
                "revision_files": ["slugkit/__init__.py"],
                "human_change_requests": ["Convert Caf\x00e9 accents"],
            },
        )
        self.assertEqual(request.human_change_requests, ["Convert Cafe9 accents"])
        self.assertEqual(request.work_order_body, "# WO-903 Café")
        self.assertEqual(request.review_feedback, "'Café' must stay")
        self.assertIsNone(request.qa_feedback)
        self.assertEqual(request.revision_files, ["slugkit/__init__.py"])

    def test_tool_strips_nul_bytes_from_unbound_arguments(self):
        request = self._run_tool_bound({"work_order_body": "# WO-903 Caf\x00e9", "review_feedback": "fix Caf\x00e9"}, {})
        self.assertEqual(request.work_order_body, "# WO-903 Cafe9")
        self.assertEqual(request.review_feedback, "fix Cafe9")

    def test_delivery_binding_reads_the_latest_deliver_task(self):
        from ts_harness.tools.coding_backend_tool import delivery_binding

        client = MagicMock()
        client.get_workflow.return_value = {
            "tasks": [
                {
                    "step_name": "deliver_work_order",
                    "input": {"repo_url": "https://github.com/old/repo"},
                },
                {
                    "step_name": "deliver_work_order",
                    "input": {
                        "repo_url": "https://github.com/example-org/test-ts-harness-repo",
                        "base_branch": "main",
                        "pr_url": "https://github.com/example-org/test-ts-harness-repo/pull/19",
                    },
                },
            ]
        }
        ctx = ToolPolicyContext(
            tenant_id="default", agent_id="dev-agent", source="test", linked_workflow_id="wf-306"
        )
        self.assertEqual(
            delivery_binding(ctx, client=client),
            {
                "repo_url": "https://github.com/example-org/test-ts-harness-repo",
                "base_branch": "main",
                "pr_url": "https://github.com/example-org/test-ts-harness-repo/pull/19",
            },
        )

    def test_tool_keeps_main_for_a_repo_other_than_the_configured_one(self):
        from ts_harness.implementation.backends.base import CodingResult

        _result, captured = self._run_tool(
            {
                "work_order_id": "WO-101",
                "work_order_body": "# WO-101\n",
                "repo_url": "https://github.com/acme/harness",
                "base_branch": "main",
            },
            CodingResult(ok=True, branch="wo/wo-101", summary="ok"),
        )
        self.assertEqual((captured[0].repo_url, captured[0].base_branch), ("https://github.com/acme/harness", "main"))

    def test_tool_ignores_configured_branch_for_another_repo_without_a_branch(self):
        from ts_harness.implementation.backends.base import CodingResult

        _result, captured = self._run_tool(
            {"work_order_id": "WO-101", "work_order_body": "# WO-101\n", "repo_url": "https://github.com/acme/harness"},
            CodingResult(ok=True, branch="wo/wo-101", summary="ok"),
        )
        self.assertEqual(captured[0].base_branch, "main")

    def test_tool_prefers_configured_branch_over_echoed_main_for_the_configured_repo(self):
        from ts_harness.implementation.backends.base import CodingResult

        _result, captured = self._run_tool(
            {
                "work_order_id": "WO-101",
                "work_order_body": "# WO-101\n",
                "repo_url": "https://github.com/Example/app",
                "base_branch": "main",
            },
            CodingResult(ok=True, branch="wo/wo-101", summary="ok"),
        )
        self.assertEqual(captured[0].base_branch, "dev")

    def test_tool_refresh_of_another_repo_keeps_main(self):
        from ts_harness.implementation.backends.base import CodingResult

        _result, captured = self._run_tool(
            {
                "work_order_id": "WO-101",
                "work_order_body": "# WO-101\n",
                "mode": "refresh",
                "pr_url": "https://github.com/acme/harness/pull/4",
                "base_branch": "main",
            },
            CodingResult(ok=True, branch="wo/wo-101", summary="ok"),
        )
        self.assertEqual((captured[0].repo_url, captured[0].base_branch), ("https://github.com/acme/harness.git", "main"))

    def _run_bound_tool(self, args, bound):
        from ts_harness.implementation.backends.base import CodingResult

        with patch("ts_harness.tools.coding_backend_tool.delivery_binding", return_value=bound):
            return self._run_tool(args, CodingResult(ok=True, branch="wo/wo-002", summary="ok"))

    def test_tool_rejects_refresh_when_the_delivery_task_asked_for_a_revision(self):
        result, captured = self._run_bound_tool(
            {"work_order_id": "WO-002", "work_order_body": "# WO-002\n", "mode": "refresh"},
            {"pr_url": "https://github.com/org/app/pull/18", "review_feedback": "fix the empty state"},
        )
        self.assertFalse(result["ok"])
        self.assertIn("mode implement", result["error"])
        self.assertEqual(captured, [])

    def test_tool_allows_refresh_when_the_delivery_task_asked_for_one(self):
        result, captured = self._run_bound_tool(
            {"work_order_id": "WO-002", "work_order_body": "# WO-002\n", "mode": "refresh"},
            {"pr_url": "https://github.com/org/app/pull/18", "refresh": True},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(captured[0].mode, "refresh")

    def test_delivery_binding_reports_a_refresh_request(self):
        from ts_harness.tools.coding_backend_tool import delivery_binding

        client = MagicMock()
        client.get_workflow.return_value = {
            "tasks": [
                {
                    "step_name": "deliver_work_order",
                    "input": {"repo_url": "https://github.com/org/app", "refresh": True},
                }
            ]
        }
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test", linked_workflow_id="wf-1")
        self.assertTrue(delivery_binding(ctx, client=client)["refresh"])

    def test_tool_refresh_requires_pr_url(self):
        from ts_harness.implementation.backends.base import CodingResult

        result, captured = self._run_tool(
            {"work_order_id": "WO-003", "work_order_body": "# WO-003\n", "mode": "refresh"},
            CodingResult(ok=True, branch="", summary=""),
        )
        self.assertFalse(result["ok"])
        self.assertIn("pr_url", result["error"])
        self.assertEqual(captured, [])

    def test_tool_builds_security_config_from_program_defaults_and_returns_block(self):
        from ts_harness.implementation.backends.base import CodingResult

        backend_kwargs: list[dict] = []
        security = {"secret_scan": {"tool": "builtin", "findings": []}}

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                backend_kwargs.append(kwargs)

            def implement(self, request):
                return CodingResult(ok=True, branch="wo/wo-003", summary="ok", pr_url="https://github.com/org/app/pull/8", security=security)

        live = MagicMock(
            program_defaults={"merge_readiness": {"policy": {"host_security": {"dependency_audit": False, "audit_severity": "critical"}}}},
            identity=MagicMock(github_logins={}),
        )
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test")
        repo = MagicMock(url="https://github.com/example/app.git", base_branch="dev", clone_depth=1)
        backend_cfg = MagicMock(provider="anthropic", model="m", binary="opencode", run_args=["run"])
        with (
            patch("ts_harness.implementation.coding_job.load_active_runtime_config", return_value=MagicMock()),
            patch("ts_harness.implementation.coding_job._live_runtime_config", return_value=live),
            patch(
                "ts_harness.implementation.coding_job.implementation_config",
                return_value=MagicMock(coding_backend="opencode", tools_root="/tools"),
            ),
            patch("ts_harness.implementation.coding_job.repo_for_workflow", return_value=repo),
            patch("ts_harness.implementation.coding_job.opencode_backend_config", return_value=backend_cfg),
            patch("ts_harness.implementation.coding_job.check_repo_allowed", return_value=None),
            patch("ts_harness.implementation.coding_job.OpenCodeBackend", FakeBackend),
        ):
            result = handle_run_coding_backend({"work_order_id": "WO-003", "work_order_body": "# WO-003\n"}, ctx)
        config = backend_kwargs[0]["security"]
        self.assertEqual((config.secret_scan, config.dependency_audit, config.audit_severity), (True, False, "critical"))
        self.assertEqual(result["security"], security)

    def test_security_scan_runs_before_push_and_lands_in_pr_body(self):
        from ts_harness.implementation.security_scan import SecurityConfig

        backend = OpenCodeBackend(security=SecurityConfig(dependency_audit=False))
        request = CodingRequest(
            repo_url="https://github.com/org/app.git",
            base_branch="main",
            work_order_id="WO-7",
            work_order_body="# WO-7\n\nchore: rename only",
            workspace_dir=Path("/tmp/ws"),
            provider="anthropic",
        )
        seen: list[dict] = []

        def fake_verify(repo_dir, **kwargs):
            from ts_harness.implementation.verification import VerifyResult

            seen.append(kwargs)
            return VerifyResult(
                ok=True, reasons=[], summary="verification passed", log_tail="",
                security={"secret_scan": {"tool": "builtin", "findings": []}, "warnings": []},
            )

        with tempfile.TemporaryDirectory() as repo_dir, patch(
            "ts_harness.implementation.backends.opencode.verify_implementation", side_effect=fake_verify
        ), patch("ts_harness.implementation.backends.opencode.list_changed_files", return_value=["a.py"]):
            failure, _log, _passes, _changed, security = backend._verify_with_fixes(
                Path(repo_dir), request, binary="opencode", provider="anthropic", env={}, branch="wo/wo-7",
                log="", log_path=Path("/tmp/log"), skills_materialized=False,
            )
        self.assertIsNone(failure)
        self.assertIs(seen[0]["security"], backend.security)
        self.assertEqual(seen[0]["base_ref"], "origin/main")
        self.assertEqual(security["secret_scan"]["tool"], "builtin")

    def test_missing_toolchain_only_skips_fix_passes(self):
        backend = OpenCodeBackend(verification_fix_passes=2)
        request = CodingRequest(
            repo_url="https://github.com/org/app.git",
            base_branch="main",
            work_order_id="WO-7",
            work_order_body="# WO-7\n\nAdd the endpoint",
            workspace_dir=Path("/tmp/ws"),
            provider="anthropic",
        )
        missing = VerifyResult(ok=False, reasons=["missing_toolchain:cargo"], summary="verification failed", log_tail="")
        test_env = TestEnvironment(python="python3")
        with tempfile.TemporaryDirectory() as repo_dir, patch(
            "ts_harness.implementation.backends.opencode.verify_implementation", return_value=missing
        ), patch("ts_harness.implementation.backends.opencode.list_changed_files", return_value=["src/main.rs"]), patch(
            "ts_harness.implementation.backends.opencode.install_declared_dependencies", return_value="npm ci ok"
        ) as install, patch.object(backend, "_run_opencode") as run_opencode:
            failure, _log, passes, _changed, _security = backend._verify_with_fixes(
                Path(repo_dir), request, binary="opencode", provider="anthropic", env={}, branch="wo/wo-7",
                log="", log_path=Path("/tmp/log"), skills_materialized=False, test_env=test_env,
            )
        self.assertEqual(passes, 0)
        run_opencode.assert_not_called()
        install.assert_called_once()
        self.assertIn("missing_toolchain:cargo", failure.error)
        self.assertIn("npm ci ok", failure.log_tail)


class RevisionScopePushTests(unittest.TestCase):
    def test_out_of_scope_revision_is_not_pushed(self):
        def runner(cmd, **_kwargs):
            completed = MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["git", "rev-parse"]:
                completed.stdout = "abc123\n"
            if cmd[:3] == ["git", "diff", "--name-only"]:
                completed.stdout = "requirements-dev.txt\n"
            return completed

        backend = OpenCodeBackend(runner=runner, verification_fix_passes=0)
        request = CodingRequest(
            repo_url="https://github.com/org/app.git",
            base_branch="main",
            work_order_id="WO-8",
            work_order_body="fix the boundary",
            workspace_dir=Path("/tmp/ws"),
            provider="anthropic",
            revision_files=["app/pricing_client.py"],
        )
        pushed: list[str] = []
        with (
            patch(
                "ts_harness.implementation.backends.opencode.resolve_coding_backend_credentials",
                return_value=("anthropic", "key"),
            ),
            patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test"}),
            patch("ts_harness.implementation.backends.opencode.ensure_repo_clone", return_value=Path("/tmp/repo")),
            patch("ts_harness.implementation.backends.opencode.create_work_branch"),
            patch.object(backend, "_prepare_test_env", return_value=(TestEnvironment(python="python3"), "")),
            patch("ts_harness.implementation.backends.opencode.install_declared_dependencies", return_value=""),
            patch("ts_harness.implementation.backends.opencode.resolve_opencode_binary", return_value="opencode"),
            patch.object(backend, "_run_opencode", return_value=MagicMock(returncode=0, stdout="", stderr="")),
            patch(
                "ts_harness.implementation.backends.opencode.verify_implementation",
                return_value=VerifyResult(ok=True, reasons=[], summary="ok", log_tail=""),
            ),
            patch("ts_harness.implementation.backends.opencode.commit_all_changes", return_value=True),
            patch(
                "ts_harness.implementation.backends.opencode.list_changed_files",
                return_value=["requirements-dev.txt"],
            ),
            patch(
                "ts_harness.implementation.backends.opencode.push_branch",
                side_effect=lambda *_args, **_kwargs: pushed.append("pushed"),
            ),
        ):
            result = backend.implement(request)
        self.assertFalse(result.ok)
        self.assertTrue(str(result.error).startswith("revision_scope:"))
        self.assertEqual(pushed, [])


class WorkspaceOwnershipTests(unittest.TestCase):
    def test_workspace_is_claimed_for_the_delivery_before_cloning(self):
        order: list[str] = []
        backend = OpenCodeBackend(runner=lambda *_a, **_k: MagicMock(returncode=0, stdout="", stderr=""))
        request = CodingRequest(
            repo_url="https://github.com/org/app.git",
            base_branch="main",
            work_order_id="WO-8",
            work_order_body="build it",
            workspace_dir=Path("/tmp/ws"),
            provider="anthropic",
            delivery_workflow_id="wf-8",
        )

        def stop_clone(**_kwargs):
            order.append("clone")
            raise RuntimeError("stop")

        with (
            patch(
                "ts_harness.implementation.backends.opencode.resolve_coding_backend_credentials",
                return_value=("anthropic", "key"),
            ),
            patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test"}),
            patch("ts_harness.implementation.backends.opencode.resolve_opencode_binary", return_value="opencode"),
            patch(
                "ts_harness.implementation.backends.opencode.claim_workspace",
                side_effect=lambda workspace, owner: order.append(f"claim:{workspace}:{owner}"),
            ),
            patch("ts_harness.implementation.backends.opencode.ensure_repo_clone", side_effect=stop_clone),
        ):
            result = backend.implement(request)
        self.assertFalse(result.ok)
        self.assertEqual(order, ["claim:/tmp/ws:wf-8", "clone"])

    def test_work_branch_is_keyed_by_the_delivery_workflow(self):
        branches: list[str] = []
        backend = OpenCodeBackend(runner=lambda *_a, **_k: MagicMock(returncode=0, stdout="", stderr=""))
        request = CodingRequest(
            repo_url="https://github.com/org/app.git",
            base_branch="main",
            work_order_id="WO-8",
            work_order_body="build it",
            workspace_dir=Path("/tmp/ws"),
            provider="anthropic",
            delivery_workflow_id="4acd196e-3005-4fa0-b947-0828ea79cbbb",
        )

        def stop_branch(_repo_dir, *, branch, **_kwargs):
            branches.append(branch)
            raise RuntimeError("stop")

        with (
            patch(
                "ts_harness.implementation.backends.opencode.resolve_coding_backend_credentials",
                return_value=("anthropic", "key"),
            ),
            patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test"}),
            patch("ts_harness.implementation.backends.opencode.resolve_opencode_binary", return_value="opencode"),
            patch("ts_harness.implementation.backends.opencode.claim_workspace"),
            patch("ts_harness.implementation.backends.opencode.ensure_repo_clone", return_value=Path("/tmp/repo")),
            patch("ts_harness.implementation.backends.opencode.create_work_branch", side_effect=stop_branch),
        ):
            backend.implement(request)
        self.assertEqual(branches, ["wo/wo-8-4acd196e"])


class ClosedRevisionTargetTests(unittest.TestCase):
    def _implement(self, pulls, delivery_workflow_id=None):
        github = MagicMock()
        github.get_pull.side_effect = pulls
        backend = OpenCodeBackend(runner=lambda *_a, **_k: MagicMock(returncode=0, stdout="", stderr=""), github=github)
        request = CodingRequest(
            repo_url="https://github.com/org/app.git",
            base_branch="main",
            work_order_id="WO-8",
            work_order_body="rename the flag",
            workspace_dir=Path("/tmp/ws"),
            provider="anthropic",
            delivery_workflow_id=delivery_workflow_id,
            pr_url="https://github.com/org/app/pull/8",
            review_feedback="Human reviewer alice requested changes",
        )
        calls = {"opencode": 0, "push": 0, "pr": 0, "pushed": []}

        def count(name, value=None):
            def inner(*_args, **kwargs):
                calls[name] += 1
                if name == "push":
                    calls["pushed"].append(kwargs.get("branch"))
                return value

            return inner

        with (
            patch(
                "ts_harness.implementation.backends.opencode.resolve_coding_backend_credentials",
                return_value=("anthropic", "key"),
            ),
            patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test"}),
            patch("ts_harness.implementation.backends.opencode.ensure_repo_clone", return_value=Path("/tmp/repo")),
            patch("ts_harness.implementation.backends.opencode.create_work_branch"),
            patch.object(backend, "_prepare_test_env", return_value=(TestEnvironment(python="python3"), "")),
            patch("ts_harness.implementation.backends.opencode.install_declared_dependencies", return_value=""),
            patch("ts_harness.implementation.backends.opencode.resolve_opencode_binary", return_value="opencode"),
            patch.object(backend, "_run_opencode", side_effect=count("opencode", MagicMock(returncode=0, stdout="", stderr=""))),
            patch(
                "ts_harness.implementation.backends.opencode.verify_implementation",
                return_value=VerifyResult(ok=True, reasons=[], summary="ok", log_tail=""),
            ),
            patch("ts_harness.implementation.backends.opencode.commit_all_changes", return_value=True),
            patch("ts_harness.implementation.backends.opencode.list_changed_files", return_value=["app/cli.py"]),
            patch("ts_harness.implementation.backends.opencode.push_branch", side_effect=count("push")),
            patch("ts_harness.implementation.backends.opencode.create_pull_request", side_effect=count("pr", "https://github.com/org/app/pull/9")),
        ):
            return backend.implement(request), calls

    def test_closed_pr_is_not_revised(self):
        result, calls = self._implement([{"state": "closed", "merged": False}])
        self.assertFalse(result.ok)
        self.assertTrue(str(result.error).startswith("pr_closed:"))
        self.assertEqual((calls["opencode"], calls["push"], calls["pr"]), (0, 0, 0))

    def test_revision_pushes_to_the_open_pr_head_branch(self):
        open_pr = {"state": "open", "merged": False, "head": {"ref": "wo/wo-8"}}
        _result, calls = self._implement([open_pr, open_pr], delivery_workflow_id="4acd196e-0000")
        self.assertEqual(calls["pushed"], ["wo/wo-8"])

    def test_pr_closed_during_the_revision_opens_no_replacement(self):
        result, calls = self._implement([{"state": "open", "merged": False}, {"state": "closed", "merged": False}])
        self.assertFalse(result.ok)
        self.assertTrue(str(result.error).startswith("pr_closed:"))
        self.assertEqual((calls["push"], calls["pr"]), (0, 0))
        self.assertGreater(calls["opencode"], 0)

    def test_open_pr_is_revised_and_pushed(self):
        result, calls = self._implement([{"state": "open", "merged": False}] * 2)
        self.assertTrue(result.ok, result.error)
        self.assertEqual((calls["push"], calls["pr"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
