"""Hardening tests for work-order delivery gaps (#1–#7)."""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.implementation.repo import create_pull_request
from ts_harness.implementation.work_order_context import (
    extract_work_order_section,
    parse_work_order_dependencies,
    select_next_work_order_id,
)
from ts_harness.tools.coding_backend_tool import handle_run_coding_backend
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.workflows.delivery_program import DeliveryProgramService
from ts_harness.workflows.hooks import complete_program_phase, wait_external
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.registry import HookContext


class ChangedFilesListingTests(unittest.TestCase):
    def test_lists_files_inside_new_untracked_directories(self):
        import subprocess
        import tempfile

        from ts_harness.implementation.repo import list_changed_files
        from ts_harness.implementation.verification import validate_work_order_changeset

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app.py").write_text("x = 1\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                check=True,
            )
            (repo / "features" / "steps").mkdir(parents=True)
            (repo / "features" / "WO-101-pricing.feature").write_text("Feature: x\n")
            (repo / "features" / "steps" / "wo_101_steps.py").write_text("pass\n")
            (repo / "app.py").write_text("x = 2\n")

            changed = list_changed_files(repo)

        self.assertIn("features/WO-101-pricing.feature", changed)
        self.assertIn("features/steps/wo_101_steps.py", changed)
        body = "# WO-101\n\n## E2E Acceptance Tests\n- Given a region, When fetched, Then a rate is returned.\n"
        self.assertNotIn(
            "missing_bdd_feature_for_work_order",
            validate_work_order_changeset(changed, work_order_id="WO-101", work_order_body=body),
        )

    def test_revision_includes_files_committed_on_the_branch(self):
        import subprocess
        import tempfile

        from ts_harness.implementation.repo import list_changed_files

        def git(repo, *args):
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            (repo / "app.py").write_text("x = 1\n")
            (repo / "old.py").write_text("y = 1\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "init")
            git(repo, "checkout", "-qb", "wo/wo-101")
            (repo / "features").mkdir()
            (repo / "features" / "WO-101-pricing.feature").write_text("Feature: x\n")
            (repo / "old.py").unlink()
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "first pass")
            (repo / "app.py").write_text("x = 2\n")

            self.assertEqual(list_changed_files(repo), ["app.py"])
            self.assertEqual(sorted(list_changed_files(repo, base_ref="main")), ["app.py", "features/WO-101-pricing.feature"])


class IdempotentPullRequestTests(unittest.TestCase):
    def _runner(self, calls: list[list[str]], view_state: str):
        def runner(cmd, **kwargs):
            calls.append(list(cmd))
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "a pull request already exists for wo/wo-001"
            if cmd[:3] == ["gh", "pr", "create"]:
                return result
            if cmd[:3] == ["gh", "pr", "view"]:
                result.returncode = 0
                result.stdout = json.dumps({"url": "https://github.com/example/app/pull/42", "state": view_state}) + "\n"
                result.stderr = ""
                return result
            if cmd[:3] == ["gh", "pr", "list"]:
                result.returncode = 0
                result.stdout = "[]\n"
                result.stderr = ""
                return result
            return result

        return runner

    def _create(self, runner):
        return create_pull_request(
            Path("/tmp/repo"),
            title="WO-001: governed delivery",
            body="body",
            base_branch="main",
            head_branch="wo/wo-001",
            runner=runner,
        )

    def test_create_pull_request_reuses_existing_pr_url(self):
        calls: list[list[str]] = []
        url = self._create(self._runner(calls, "OPEN"))
        self.assertEqual(url, "https://github.com/example/app/pull/42")
        self.assertTrue(any(c[:3] == ["gh", "pr", "view"] for c in calls))

    def test_create_pull_request_does_not_reuse_merged_pr(self):
        with self.assertRaisesRegex(RuntimeError, "gh pr create failed"):
            self._create(self._runner([], "MERGED"))


class WorkOrderIdAndDependencyTests(unittest.TestCase):
    def test_extract_supports_non_three_digit_ids(self):
        md = "# WO-1 Small\n\nBody one.\n\n# WO-1000 Large\n\nBody large.\n"
        self.assertIn("Body one", extract_work_order_section(md, "WO-1"))
        self.assertIn("Body large", extract_work_order_section(md, "WO-1000"))

    def test_parse_depends_on_from_context_graph(self):
        md = (
            "# WO-001 Foundation\n\n"
            "| **Depends on** | None |\n\n"
            "# WO-002 Feature\n\n"
            "| **Depends on** | WO-001 |\n\n"
            "# WO-003 Follow-up\n\n"
            "| **Depends on** | WO-001, WO-002 |\n"
        )
        deps = parse_work_order_dependencies(md)
        self.assertEqual(deps.get("WO-001"), [])
        self.assertEqual(deps.get("WO-002"), ["WO-001"])
        self.assertEqual(deps.get("WO-003"), ["WO-001", "WO-002"])

    def test_select_next_respects_dependencies(self):
        ids = ["WO-001", "WO-002", "WO-003"]
        deps = {"WO-001": [], "WO-002": ["WO-001"], "WO-003": ["WO-002"]}
        self.assertEqual(select_next_work_order_id(ids, [], deps), "WO-001")
        self.assertEqual(select_next_work_order_id(ids, ["WO-001"], deps), "WO-002")
        # WO-003 blocked until WO-002 done even if listed first among remaining.
        self.assertEqual(
            select_next_work_order_id(["WO-003", "WO-002"], ["WO-001"], deps),
            "WO-002",
        )
        self.assertIsNone(select_next_work_order_id(["WO-003"], ["WO-001"], deps))


class MergeShaResumeTests(unittest.TestCase):
    def test_wait_external_resume_includes_merge_sha_aliases(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-1", "workflow_type": "implement_work_orders"},
            "tasks": [
                {
                    "id": "t-deliver",
                    "step_name": "deliver_work_order",
                    "status": "succeeded",
                    "output": {"pr_url": "https://github.com/o/r/pull/1"},
                },
                {
                    "id": "t-qa",
                    "step_name": "qa_gate",
                    "status": "succeeded",
                    "output": {
                        "result": {
                            "prs": [{"pr_url": "https://github.com/o/r/pull/1"}],
                        }
                    },
                },
                {
                    "id": "t-wait",
                    "step_name": "await_human_merge",
                    "status": "blocked",
                    "error": "awaiting_external:github.merge",
                    "expected_event_type": "github.merge",
                },
            ],
        }
        client.resume_external.return_value = {"status": "resumed", "task_id": "t-wait"}
        skeleton = load_skeleton("deliver_work_order")
        ctx = HookContext(
            workflow_id="wf-1",
            workflow_type="deliver_work_order",
            tenant_id="default",
            task={"id": "t-wait"},
            stage=skeleton.stage("await_human_merge"),
            skeleton=skeleton,
            actor="github-webhook",
            memory_client=client,
            extras={
                "event_type": "github.merge",
                "payload": {
                    "pr_url": "https://github.com/o/r/pull/1",
                    "merge_commit_sha": "abc123def",
                },
            },
        )
        wait_external.resume(ctx)
        kwargs = client.resume_external.call_args.kwargs
        output = kwargs.get("task_output") or {}
        self.assertEqual(output.get("merge_sha"), "abc123def")
        self.assertEqual(output.get("pr_url"), "https://github.com/o/r/pull/1")


class TerminalAndReentryTests(unittest.TestCase):
    def test_complete_program_phase_marks_terminal_when_no_remaining_wos(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-impl-wo",
                "workflow_type": "implement_work_orders",
                "program_id": "prog-smoke",
                "program_phase": "implement",
                "summary": "WO-001",
            },
            "tasks": [
                {"status": "succeeded", "output": {"work_order_id": "WO-001", "pr_url": "https://example/pr/1"}},
                {"status": "succeeded", "output": {"merge_sha": "deadbeef"}},
            ],
        }
        client.get_program.return_value = {
            "program": {
                "current_phase": "implement",
                "metadata": {
                    "artifacts": {
                        "work_order_ids": ["WO-001"],
                        "completed_work_order_ids": ["WO-001"],
                        "planning_track": "work_orders",
                    },
                },
            },
        }
        client.patch_program_artifacts.return_value = {}
        service = DeliveryProgramService(client=client)
        skeleton = load_skeleton("implement_work_orders")
        ctx = HookContext(
            workflow_id="wf-impl-wo",
            workflow_type="implement_work_orders",
            tenant_id="default",
            task={"id": "t-final"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )
        with patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
            return_value=service,
        ), patch(
            "ts_harness.workflows.hooks.notify_bound_channel.handle",
            return_value={"status": "notified"},
        ) as notify:
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "phase_completed_work_orders_done")
        artifact_patches = [
            call.args[1]
            for call in client.patch_program_artifacts.call_args_list
            if len(call.args) >= 2 and isinstance(call.args[1], dict)
        ]
        self.assertTrue(
            any(p.get("program_status") == "succeeded" for p in artifact_patches)
            or any(p.get("work_orders_delivery_complete") for p in artifact_patches),
            artifact_patches,
        )
        client.complete_workflow.assert_called_once()
        notify.assert_called_once()
        channel_text = str(ctx.extras.get("channel_text") or "")
        self.assertIn("Implement is complete", channel_text)
        self.assertIn("All work orders", channel_text)
        self.assertNotIn("deploy_release", channel_text.lower())
        self.assertNotIn("Deploy the latest", channel_text)

    def test_reentry_skips_wo_with_unmet_dependencies(self):
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-impl-wo",
                "workflow_type": "implement_work_orders",
                "program_id": "prog-smoke",
                "program_phase": "implement",
                "summary": "WO-001",
            },
            "tasks": [
                {"status": "succeeded", "output": {"work_order_id": "WO-001", "pr_url": "https://example/pr/1"}},
                {"status": "succeeded"},
            ],
        }
        markdown = (
            "# WO-001 A\n\n| **Depends on** | None |\n\n"
            "# WO-002 B\n\n| **Depends on** | WO-003 |\n\n"
            "# WO-003 C\n\n| **Depends on** | WO-001 |\n"
        )
        client.get_program.return_value = {
            "program": {
                "current_phase": "implement",
                "metadata": {
                    "artifacts": {
                        "work_order_ids": ["WO-001", "WO-002", "WO-003"],
                        "completed_work_order_ids": ["WO-001"],
                        "work_orders_markdown": markdown,
                        "planning_track": "work_orders",
                    },
                },
            },
        }
        client.advance_program.return_value = {"current_phase": "implement"}
        client.create_workflow.return_value = {"id": "wf-impl-wo-2"}
        client.create_task.return_value = {"id": "task-2"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-smoke"}
        client.patch_program_artifacts.return_value = {}

        service = DeliveryProgramService(client=client)
        skeleton = load_skeleton("implement_work_orders")
        ctx = HookContext(
            workflow_id="wf-impl-wo",
            workflow_type="implement_work_orders",
            tenant_id="default",
            task={"id": "t-final"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )
        with patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
            return_value=service,
        ):
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "phase_reentered_implement")
        client.complete_workflow.assert_called_once()
        create_task_inputs = [
            call.args[0].get("input") or {}
            for call in client.create_task.call_args_list
            if call.args
        ]
        self.assertTrue(
            any(inp.get("work_order_id") == "WO-003" for inp in create_task_inputs),
            create_task_inputs,
        )


class AutoAdvanceAndCodingFallbackTests(unittest.TestCase):
    def test_reentry_uses_node_config_for_cloned_workflow_type(self):
        """Reentry must not hardcode workflow_type == implement_work_orders."""
        from program_legacy_compat import frozen_legacy_compatibility_topology

        snapshot = frozen_legacy_compatibility_topology()
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-impl-clone",
                "workflow_type": "implement_work_orders_clone",
                "program_id": "prog-clone",
                "program_phase": "implement",
                "summary": "WO-001",
            },
            "tasks": [
                {
                    "status": "succeeded",
                    "output": {
                        "work_order_id": "WO-001",
                        "pr_url": "https://example/pr/1",
                    },
                },
                {"status": "succeeded"},
            ],
        }
        client.get_program.return_value = {
            "program": {
                "current_phase": "implement",
                "program_definition_snapshot": snapshot,
                "metadata": {
                    "artifacts": {
                        "work_order_ids": ["WO-001", "WO-002"],
                        "completed_work_order_ids": ["WO-001"],
                        "planning_track": "work_orders",
                    },
                },
            },
        }
        client.advance_program.return_value = {
            "current_phase": "implement",
            "workflow_id": "wf-impl-clone-2",
        }
        client.create_workflow.return_value = {"id": "wf-impl-clone-2"}
        client.create_task.return_value = {"id": "task-2"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-clone"}
        client.patch_program_artifacts.return_value = {}

        service = DeliveryProgramService(client=client)
        skeleton = load_skeleton("implement_work_orders")
        ctx = HookContext(
            workflow_id="wf-impl-clone",
            workflow_type="implement_work_orders_clone",
            tenant_id="default",
            task={"id": "t-final"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )
        with patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
            return_value=service,
        ):
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "phase_reentered_implement")
        client.complete_workflow.assert_called_once()

    def test_work_orders_from_prd_auto_advances_to_implement(self):
        skeleton = load_skeleton("work_orders_from_prd")
        self.assertTrue(skeleton.program.get("auto_advance"))
        self.assertEqual(skeleton.program.get("next_phase"), "implement")

    def test_coding_tool_falls_back_to_markdown_section(self):
        md = "# WO-001 Lobby\n\nImplement lobby.\n\n# WO-002 Other\n\nNope.\n"
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test")
        fake_result = MagicMock()
        fake_result.ok = True
        fake_result.branch = "wo/wo-001"
        fake_result.summary = "ok"
        fake_result.changed_files = ["a.py"]
        fake_result.log_tail = ""
        fake_result.error = None
        fake_result.pr_url = "https://github.com/example/app/pull/1"

        with (
            patch(
                "ts_harness.implementation.coding_job.load_active_runtime_config",
                return_value=None,
            ),
            patch(
                "ts_harness.implementation.coding_job.implementation_config",
                return_value=MagicMock(
                    coding_backend="opencode",
                    tools_root="/tools",
                ),
            ),
            patch(
                "ts_harness.implementation.coding_job.repo_for_workflow",
                return_value=MagicMock(url="https://github.com/example/app.git", base_branch="main", clone_depth=1),
            ),
            patch(
                "ts_harness.implementation.coding_job.opencode_backend_config",
                return_value=MagicMock(binary="opencode", provider="anthropic", model=None, run_args=["run", "--auto"]),
            ),
            patch(
                "ts_harness.implementation.coding_job.OpenCodeBackend"
            ) as backend_cls,
        ):
            backend_cls.return_value.implement.return_value = fake_result
            result = handle_run_coding_backend(
                {
                    "work_order_id": "WO-001",
                    "work_orders_markdown": md,
                    "repo_url": "https://github.com/example/app.git",
                },
                ctx,
            )
        self.assertTrue(result["ok"])
        request = backend_cls.return_value.implement.call_args.args[0]
        self.assertIn("Implement lobby", request.work_order_body)

    def test_coding_tool_prefers_configured_base_branch_over_main_arg(self):
        ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="test")
        fake_result = MagicMock(
            ok=True,
            branch="wo/wo-001",
            summary="ok",
            changed_files=[],
            log_tail="",
            error=None,
            pr_url="https://github.com/example/app/pull/2",
        )
        with (
            patch(
                "ts_harness.implementation.coding_job.load_active_runtime_config",
                return_value=None,
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
                    binary="opencode",
                    provider="anthropic",
                    model=None,
                    run_args=["run"],
                ),
            ),
            patch("ts_harness.implementation.coding_job.OpenCodeBackend") as backend_cls,
        ):
            backend_cls.return_value.implement.return_value = fake_result
            result = handle_run_coding_backend(
                {
                    "work_order_id": "WO-001",
                    "work_order_body": "# WO-001\n\nDo the thing.\n",
                    "base_branch": "main",
                },
                ctx,
            )
        self.assertTrue(result["ok"])
        request = backend_cls.return_value.implement.call_args.args[0]
        self.assertEqual(request.base_branch, "dev")
        self.assertEqual(result["base_branch"], "dev")


class GitHubWebhookTests(unittest.TestCase):
    def test_parse_pull_request_merged_event(self):
        from ts_harness.gateway.github_webhook import parse_github_merge_event

        body = {
            "action": "closed",
            "pull_request": {
                "merged": True,
                "html_url": "https://github.com/org/app/pull/9",
                "merge_commit_sha": "abc123",
                "base": {"ref": "main"},
                "head": {"ref": "wo/wo-001"},
            },
        }
        event = parse_github_merge_event(body)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event["pr_url"], "https://github.com/org/app/pull/9")
        self.assertEqual(event["merge_sha"], "abc123")

    def test_verify_signature(self):
        from ts_harness.gateway.github_webhook import verify_github_signature

        secret = "test-secret"
        raw = b'{"ok":true}'
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        self.assertTrue(verify_github_signature(raw, f"sha256={digest}", secret))
        self.assertFalse(verify_github_signature(raw, "sha256=deadbeef", secret))

    def test_handle_github_webhook_resumes_matching_wait(self):
        from ts_harness.gateway.github_webhook import handle_github_webhook

        body = {
            "action": "closed",
            "pull_request": {
                "merged": True,
                "html_url": "https://github.com/org/app/pull/9",
                "merge_commit_sha": "abc123",
            },
        }
        engine = MagicMock()
        engine.resume_external.return_value = {"status": "resumed", "task_id": "t-wait"}
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [
                {
                    "workflow_id": "wf-1",
                    "task_id": "t-wait",
                    "expected_event_type": "github.merge",
                    "match_pr_url": "https://github.com/org/app/pull/9",
                }
            ]
        }
        with (
            patch("ts_harness.gateway.github_webhook.get_workflow_engine", return_value=engine),
            patch("ts_harness.gateway.github_webhook.get_memory_client", return_value=memory),
        ):
            result = handle_github_webhook(body, tenant_id="default")
        self.assertEqual(result["status"], "resumed")
        engine.resume_external.assert_called_once()
        call_kwargs = engine.resume_external.call_args.kwargs
        self.assertEqual(call_kwargs["event_type"], "github.merge")
        self.assertEqual(call_kwargs["payload"]["pr_url"], "https://github.com/org/app/pull/9")
        self.assertEqual(call_kwargs["payload"]["merge_sha"], "abc123")


if __name__ == "__main__":
    unittest.main()
