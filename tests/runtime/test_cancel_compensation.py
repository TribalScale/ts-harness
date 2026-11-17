"""Cancel compensation over program_external_refs, its settings, and the portfolio BFF."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.config.runtime_settings import _canonicalize_program_defaults
from ts_harness.gateway import asgi_app
from ts_harness.gateway.admin_bff import build_portfolio
from ts_harness.integrations.github_client import GitHubError
from ts_harness.integrations.atlassian_auth import TOKEN_URL, AtlassianAuth
from ts_harness.integrations.jira_client import JiraClient, JiraError
from ts_harness.workflows.operability import build_operability_status
from ts_harness.workflows.program_compensation import (
    CANCELLED_LABEL,
    compensate_cancelled_program,
    compensation_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PR_URL = "https://github.com/acme/app/pull/7"
BOUND = {"github": {"org": "acme"}}


def _ref(kind: str, ref: str, state: str = "open", **metadata: Any) -> dict[str, Any]:
    return {"program_id": "child-1", "kind": kind, "ref": ref, "state": state, "metadata": metadata}


def _memory(refs: list[dict[str, Any]]) -> mock.MagicMock:
    memory = mock.MagicMock()
    memory.list_program_external_refs.return_value = {"refs": refs}
    return memory


def _github(pull: dict[str, Any] | None = None) -> mock.MagicMock:
    github = mock.MagicMock()
    github.get_pull.return_value = pull if pull is not None else {"state": "open", "merged": False}
    return github


def _results(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(item["kind"], item["action"], item["result"]) for item in result["actions"]]


class CompensationSettingsTests(unittest.TestCase):
    def _canonical(self, raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
        issues: list[dict[str, str]] = []
        out = _canonicalize_program_defaults({"compensation": raw}, path="$.program_defaults", issues=issues)
        return out.get("compensation", {}), issues

    def test_valid_settings(self):
        out, issues = self._canonical({"jira": "Close", "jira_close_transition": "Won't Do", "delete_branches": True})
        self.assertEqual(issues, [])
        self.assertEqual(out, {"jira": "close", "jira_close_transition": "Won't Do", "delete_branches": True})

    def test_invalid_settings(self):
        _, issues = self._canonical({"jira": "delete", "delete_branches": "yes", "extra": 1})
        codes = {issue["code"] for issue in issues}
        self.assertTrue({"invalid_compensation_jira", "invalid_compensation_branches"} <= codes)
        self.assertEqual(len(issues), 3)

    def test_defaults_label_jira_and_keep_branches(self):
        self.assertEqual(
            compensation_settings({}),
            {"jira": "label", "jira_close_transition": "Done", "delete_branches": False},
        )


class CompensateCancelledProgramTests(unittest.TestCase):
    def test_reads_refs_for_program_and_descendants(self):
        memory = _memory([])
        compensate_cancelled_program(memory, "root-1", github=_github(), jira=mock.MagicMock())
        memory.list_program_external_refs.assert_called_once_with("root-1", include_descendants=True)
        memory.record_program_compensation.assert_called_once_with("root-1", status="skipped", actions=[], actor="orchestrator")

    def test_jira_is_labelled_and_commented_by_default(self):
        memory = _memory([_ref("jira_issue", "PROJ-1")])
        jira = mock.MagicMock()
        result = compensate_cancelled_program(memory, "root-1", reason="scope dropped", github=_github(), jira=jira)
        jira.add_label.assert_called_once_with("PROJ-1", CANCELLED_LABEL)
        self.assertIn("scope dropped", jira.comment.call_args.args[1])
        jira.transition.assert_not_called()
        self.assertEqual(_results(result), [("jira_issue", "label_and_comment", "done")])
        self.assertEqual(memory.record_program_external_ref.call_args.kwargs["state"], "labelled")

    def test_jira_closes_only_when_configured(self):
        memory = _memory([_ref("jira_issue", "PROJ-1")])
        jira = mock.MagicMock()
        jira.transition.return_value = True
        result = compensate_cancelled_program(
            memory,
            "root-1",
            defaults={"compensation": {"jira": "close", "jira_close_transition": "Cancelled"}},
            github=_github(),
            jira=jira,
        )
        jira.transition.assert_called_once_with("PROJ-1", "Cancelled")
        self.assertEqual(_results(result), [("jira_issue", "label_comment_and_close", "done")])
        self.assertEqual(memory.record_program_external_ref.call_args.kwargs["state"], "closed")

    def test_rerun_skips_labelled_jira_and_only_retries_the_close(self):
        jira = mock.MagicMock()
        result = compensate_cancelled_program(
            _memory([_ref("jira_issue", "PROJ-1", state="labelled")]), "root-1", github=_github(), jira=jira
        )
        self.assertEqual(_results(result), [("jira_issue", "none", "skipped")])
        jira.transition.return_value = True
        compensate_cancelled_program(
            _memory([_ref("jira_issue", "PROJ-1", state="labelled")]),
            "root-1",
            defaults={"compensation": {"jira": "close"}},
            github=_github(),
            jira=jira,
        )
        jira.add_label.assert_not_called()
        jira.comment.assert_not_called()
        jira.transition.assert_called_once_with("PROJ-1", "Done")

    def test_jira_close_without_matching_transition_fails(self):
        jira = mock.MagicMock()
        jira.transition.return_value = False
        result = compensate_cancelled_program(
            _memory([_ref("jira_issue", "PROJ-1")]),
            "root-1",
            defaults={"compensation": {"jira": "close"}},
            github=_github(),
            jira=jira,
        )
        self.assertEqual(result["status"], "failed")

    def test_open_pr_is_commented_and_closed(self):
        github = _github()
        memory = _memory([_ref("pull_request", PR_URL)])
        result = compensate_cancelled_program(memory, "root-1", defaults=BOUND, github=github, jira=mock.MagicMock())
        github.comment.assert_called_once()
        self.assertEqual(github.comment.call_args.args[:3], ("acme", "app", 7))
        github.close_pull.assert_called_once_with("acme", "app", 7)
        self.assertEqual(result["status"], "completed")

    def test_pr_outside_tenant_binding_is_never_touched(self):
        for defaults in ({}, {"github": {"org": "other"}}, {"repo_picker": {"repos": [{"id": "acme/api"}]}}):
            github = _github()
            result = compensate_cancelled_program(
                _memory([_ref("pull_request", PR_URL)]), "root-1", defaults=defaults, github=github, jira=mock.MagicMock()
            )
            github.get_pull.assert_not_called()
            github.close_pull.assert_not_called()
            self.assertEqual(_results(result), [("pull_request", "none", "skipped")])
            self.assertTrue(result["actions"][0]["detail"].startswith("repo_not_bound"))

    def test_branch_outside_tenant_binding_is_never_deleted(self):
        refs = [
            _ref("pull_request", PR_URL, state="closed", branch="wo/wo-1", repo="acme/app"),
            _ref("branch", "acme/app#wo/wo-1", branch="wo/wo-1", repo="acme/app", created_by_harness=True),
        ]
        github = _github()
        result = compensate_cancelled_program(
            _memory(refs),
            "root-1",
            defaults={"compensation": {"delete_branches": True}, "github": {"org": "other"}},
            github=github,
            jira=mock.MagicMock(),
        )
        github.delete_branch.assert_not_called()
        branch = next(item for item in result["actions"] if item["kind"] == "branch")
        self.assertTrue(branch["detail"].startswith("repo_not_bound"))

    def test_jira_outside_allowed_projects_is_skipped(self):
        jira = mock.MagicMock()
        result = compensate_cancelled_program(
            _memory([_ref("jira_issue", "OPS-9")]),
            "root-1",
            defaults={"jira_filing": {"allowed_projects": ["PROJ"]}},
            github=_github(),
            jira=jira,
        )
        jira.add_label.assert_not_called()
        self.assertTrue(result["actions"][0]["detail"].startswith("project_not_bound"))

    def test_merged_pr_is_left_alone(self):
        github = _github({"state": "closed", "merged": True})
        result = compensate_cancelled_program(_memory([_ref("pull_request", PR_URL)]), "root-1", github=github, jira=mock.MagicMock())
        github.close_pull.assert_not_called()
        self.assertEqual(_results(result), [("pull_request", "none", "skipped")])

    def test_branch_kept_by_default(self):
        refs = [
            _ref("pull_request", PR_URL, branch="wo/wo-1", repo="acme/app"),
            _ref("branch", "acme/app#wo/wo-1", branch="wo/wo-1", repo="acme/app", created_by_harness=True),
        ]
        github = _github()
        result = compensate_cancelled_program(_memory(refs), "root-1", github=github, jira=mock.MagicMock())
        github.delete_branch.assert_not_called()
        self.assertIn(("branch", "none", "skipped"), _results(result))

    def test_branch_deleted_when_enabled_claw_created_and_pr_closed(self):
        refs = [
            _ref("pull_request", PR_URL, branch="wo/wo-1", repo="acme/app"),
            _ref("branch", "acme/app#wo/wo-1", branch="wo/wo-1", repo="acme/app", created_by_harness=True),
        ]
        github = _github()
        result = compensate_cancelled_program(
            _memory(refs),
            "root-1",
            defaults={"compensation": {"delete_branches": True}, **BOUND},
            github=github,
            jira=mock.MagicMock(),
        )
        github.delete_branch.assert_called_once_with("acme", "app", "wo/wo-1")
        self.assertIn(("branch", "delete_branch", "done"), _results(result))

    def test_branch_kept_when_pr_merged_or_not_claw_created(self):
        enabled = {"compensation": {"delete_branches": True}}
        merged = [
            _ref("pull_request", PR_URL, state="merged", branch="wo/wo-1", repo="acme/app"),
            _ref("branch", "acme/app#wo/wo-1", branch="wo/wo-1", repo="acme/app", created_by_harness=True),
        ]
        foreign = [
            _ref("pull_request", PR_URL, branch="feature", repo="acme/app"),
            _ref("branch", "acme/app#feature", branch="feature", repo="acme/app"),
        ]
        for refs in (merged, foreign):
            github = _github()
            compensate_cancelled_program(_memory(refs), "root-1", defaults=enabled, github=github, jira=mock.MagicMock())
            github.delete_branch.assert_not_called()

    def test_deployments_are_never_touched(self):
        github = _github()
        result = compensate_cancelled_program(
            _memory([_ref("deployment", "acme/app@abc:production")]), "root-1", github=github, jira=mock.MagicMock()
        )
        self.assertEqual(result["actions"], [])
        github.assert_not_called()

    def test_one_failure_makes_the_result_partial(self):
        github = _github()
        github.close_pull.side_effect = GitHubError(403, "forbidden")
        result = compensate_cancelled_program(
            _memory([_ref("jira_issue", "PROJ-1"), _ref("pull_request", PR_URL)]),
            "root-1",
            defaults=BOUND,
            github=github,
            jira=mock.MagicMock(),
        )
        self.assertEqual(result["status"], "partial")
        failure = next(item for item in result["actions"] if item["result"] == "failed")
        self.assertEqual(failure["kind"], "pull_request")
        self.assertIn("403", failure["detail"])

    def test_missing_jira_credentials_only_fail_jira_refs(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = compensate_cancelled_program(
                _memory([_ref("jira_issue", "PROJ-1"), _ref("pull_request", PR_URL)]),
                "root-1",
                defaults=BOUND,
                github=_github(),
            )
        self.assertEqual(
            _results(result),
            [("jira_issue", "label_and_comment", "failed"), ("pull_request", "comment_and_close", "done")],
        )


class JiraClientTests(unittest.TestCase):
    def test_label_comment_and_transition_requests(self):
        calls: list[tuple[str, str, dict[str, Any] | None]] = []

        def transport(method, url, headers, body):
            calls.append((method, url, json.loads(body) if body else None))
            if url.endswith("/transitions") and method == "GET":
                return 200, json.dumps({"transitions": [{"id": "31", "name": "Close", "to": {"name": "Done"}}]}).encode()
            return 204, b""

        auth = AtlassianAuth(mode="basic", site_url="https://acme.atlassian.net", email="bot@acme.io", api_token="t")
        client = JiraClient(auth=auth, transport=transport)
        client.add_label("PROJ-1", CANCELLED_LABEL)
        client.comment("PROJ-1", "cancelled")
        self.assertTrue(client.transition("PROJ-1", "done"))
        self.assertFalse(client.transition("PROJ-1", "Won't Do"))
        self.assertEqual(calls[0], ("PUT", "https://acme.atlassian.net/rest/api/3/issue/PROJ-1", {"update": {"labels": [{"add": CANCELLED_LABEL}]}}))
        self.assertEqual(calls[1][0:2], ("POST", "https://acme.atlassian.net/rest/api/3/issue/PROJ-1/comment"))
        self.assertEqual(calls[3], ("POST", "https://acme.atlassian.net/rest/api/3/issue/PROJ-1/transitions", {"transition": {"id": "31"}}))

    def test_accepts_three_tuple_transport(self):
        client = JiraClient(
            auth=AtlassianAuth(mode="basic", site_url="https://acme.atlassian.net", email="bot@acme.io", api_token="t"),
            transport=lambda method, url, headers, body: (200, b'{"ok": true}', {}),
        )
        self.assertEqual(client.request("GET", "/rest/api/3/myself"), {"ok": True})

    def _oauth_client(self, issue_statuses: list[int]):
        tokens: list[str] = []
        seen: list[tuple[str, str]] = []

        def transport(method, url, headers, body):
            if url == TOKEN_URL:
                tokens.append(f"tok-{len(tokens) + 1}")
                return 200, json.dumps({"access_token": tokens[-1], "expires_in": 3600}).encode(), {}
            seen.append((url, headers["Authorization"]))
            return issue_statuses.pop(0), b"", {}

        auth = AtlassianAuth(mode="oauth", client_id="id", client_secret="secret", cloud_id="cloud-1", transport=transport)
        return JiraClient(auth=auth, transport=transport), seen

    def test_oauth_uses_gateway_url_and_bearer(self):
        client, seen = self._oauth_client([204])
        client.add_label("PROJ-1", CANCELLED_LABEL)
        self.assertEqual(seen, [("https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/issue/PROJ-1", "Bearer tok-1")])

    def test_oauth_retries_once_after_401_with_fresh_token(self):
        client, seen = self._oauth_client([401, 204])
        client.add_label("PROJ-1", CANCELLED_LABEL)
        self.assertEqual([auth for _, auth in seen], ["Bearer tok-1", "Bearer tok-2"])

    def test_oauth_second_401_raises(self):
        client, seen = self._oauth_client([401, 401])
        with self.assertRaises(JiraError) as ctx:
            client.add_label("PROJ-1", CANCELLED_LABEL)
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(seen), 2)

    def test_from_env_without_credentials_raises_jira_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(JiraError) as ctx:
                JiraClient.from_env()
        self.assertEqual(ctx.exception.status, 401)


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_compensation", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GatewayCompensationRecordTests(unittest.TestCase):
    def test_record_patches_artifacts_and_audits(self):
        gateway = load_gateway()
        audits: list[str] = []
        cursor = mock.MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.execute.side_effect = lambda sql, params=None: audits.append(params[2]) if "session_audit_log" in sql else None
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        actions = [{"kind": "pull_request", "ref": PR_URL, "action": "comment_and_close", "result": "failed"}]
        with mock.patch.object(gateway, "patch_program_artifacts") as patch:
            result = gateway.record_program_compensation(
                conn, "22222222-2222-2222-2222-222222222222", {"status": "partial", "actions": actions}
            )
        artifacts = patch.call_args.args[2]["artifacts"]
        self.assertEqual(artifacts["compensation_status"], "partial")
        self.assertEqual(artifacts["compensation"]["failures"], actions)
        self.assertEqual(result["compensation"]["status"], "partial")
        self.assertEqual(audits, ["program_compensation_recorded"])

    def test_record_rejects_unknown_status(self):
        with self.assertRaisesRegex(ValueError, "status must be one of"):
            load_gateway().record_program_compensation(mock.MagicMock(), "22222222-2222-2222-2222-222222222222", {"status": "done"})


class OperabilityCompensationTests(unittest.TestCase):
    def test_compensation_failures_recommend_retry(self):
        status = build_operability_status(
            workflow={"id": "wf-1", "status": "cancelled"},
            tasks=[],
            artifacts={"compensation": {"status": "partial", "failures": [{"ref": PR_URL}]}},
        )
        self.assertEqual(status["recommended_action"], "retry_failed_cancel_compensation")
        self.assertEqual(status["compensation"]["status"], "partial")


class AdminEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.runtime.config.gateway.cors_origins = ["*"]
        self.runtime.config.program_defaults = {"compensation": {"delete_branches": True}}
        self.auth = mock.Mock(actor_id="admin-1", roles=["admin"], tenant_id="default")
        self.runtime.policy.evaluate_identity.side_effect = lambda identity, required: mock.Mock(
            decision="allow" if set(identity["roles"]) & set(required) else "deny", reason="admin or owner required"
        )
        self.memory = mock.MagicMock()
        self.memory.get_program.return_value = {"program": {"id": "prog-1", "tenant_id": "default"}}
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_cancel_runs_compensation_with_live_settings(self):
        self.memory.cancel_program.return_value = {"program_id": "prog-1", "status": "cancelled"}
        with mock.patch(
            "ts_harness.workflows.program_compensation.compensate_cancelled_program",
            return_value={"status": "completed", "actions": []},
        ) as compensate:
            response = self.client.post("/admin/programs/prog-1/cancel", json={"reason": "dup"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["compensation"], {"status": "completed", "actions": []})
        kwargs = compensate.call_args.kwargs
        self.assertEqual(kwargs["reason"], "dup")
        self.assertEqual(kwargs["defaults"], {"compensation": {"delete_branches": True}})

    def test_cancel_reports_compensation_crash_without_failing_cancel(self):
        self.memory.cancel_program.return_value = {"program_id": "prog-1", "status": "cancelled"}
        with mock.patch(
            "ts_harness.workflows.program_compensation.compensate_cancelled_program",
            side_effect=RuntimeError("boom"),
        ):
            response = self.client.post("/admin/programs/prog-1/cancel", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["compensation"]["status"], "failed")

    def test_cancel_requires_admin_role(self):
        self.auth.roles = ["member"]
        with mock.patch("ts_harness.workflows.program_compensation.compensate_cancelled_program") as compensate:
            response = self.client.post("/admin/programs/prog-1/cancel", json={})
        self.assertEqual(response.status_code, 403)
        self.memory.cancel_program.assert_not_called()
        compensate.assert_not_called()

    def test_program_of_another_tenant_is_not_found(self):
        self.memory.get_program.return_value = {"program": {"id": "prog-1", "tenant_id": "other"}}
        for method, path in (
            ("post", "/admin/programs/prog-1/cancel"),
            ("post", "/admin/programs/prog-1/advance"),
            ("get", "/admin/programs/prog-1"),
            ("get", "/admin/programs/prog-1/status"),
            ("get", "/admin/programs/prog-1/phases/implement/activity"),
        ):
            response = getattr(self.client, method)(path, **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status_code, 404, path)
        self.memory.cancel_program.assert_not_called()
        self.memory.get_program_status.assert_not_called()

    def test_workflow_of_another_tenant_is_not_found(self):
        self.memory.get_workflow_tasks.return_value = {"workflow_id": "wf-1", "tasks": [{"id": "t1", "tenant_id": "other"}]}
        self.assertEqual(self.client.get("/admin/workflows/wf-1").status_code, 404)

    def test_program_phase_activity_lists_the_stage_and_its_children(self):
        self.memory.get_program.return_value = {"program": {"id": "prog-1", "tenant_id": "default"}}
        activity = {"phase": "implement", "workflow": {"state": "Waiting"}, "children": [{"label": "WO-003"}]}
        with mock.patch("ts_harness.workflows.phase_activity.phase_activity", return_value=activity) as build:
            response = self.client.get("/admin/programs/prog-1/phases/implement/activity")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), activity)
        build.assert_called_once_with(self.memory, "prog-1", "implement")

    def test_stuck_step_decision_runs_through_the_engine_as_the_caller(self):
        self.memory.get_workflow.return_value = {"workflow": {"id": "wf-d", "tenant_id": "default"}, "tasks": []}
        engine = mock.Mock()
        engine.decide_blocked_step.return_value = {"status": "revising", "revision_number": 1}
        with mock.patch.object(asgi_app, "get_workflow_engine", return_value=engine):
            response = self.client.post(
                "/admin/workflows/decide",
                json={"workflow_id": "wf-d", "task_id": "finalize-4", "action": "revise", "guidance": "Persist empty state"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "revising")
        engine.decide_blocked_step.assert_called_once_with(
            "wf-d", "finalize-4", "revise", actor="admin-1", guidance="Persist empty state", tenant_id="default"
        )

    def test_stuck_step_decision_checks_tenant_and_state(self):
        engine = mock.Mock()
        with mock.patch.object(asgi_app, "get_workflow_engine", return_value=engine):
            self.memory.get_workflow.return_value = {"workflow": {"id": "wf-d", "tenant_id": "other"}, "tasks": []}
            body = {"workflow_id": "wf-d", "task_id": "t", "action": "accept"}
            self.assertEqual(self.client.post("/admin/workflows/decide", json=body).status_code, 404)
            engine.decide_blocked_step.assert_not_called()
            self.memory.get_workflow.return_value = {"workflow": {"id": "wf-d", "tenant_id": "default"}, "tasks": []}
            engine.decide_blocked_step.side_effect = ValueError("step is not waiting for a decision")
            response = self.client.post("/admin/workflows/decide", json=body)
            self.assertEqual(response.status_code, 409)
            self.assertIn("not waiting", response.json()["error"])
            self.assertEqual(self.client.post("/admin/workflows/decide", json={"task_id": "t"}).status_code, 400)

    def test_gate_decisions_carry_the_caller_tenant(self):
        self.memory.approve_task.return_value = {"id": "t1"}
        self.memory.reject_task.return_value = {"id": "t1"}
        self.client.post("/admin/workflows/approve", json={"id": "t1"})
        self.client.post("/admin/workflows/reject", json={"id": "t1"})
        self.assertEqual(self.memory.approve_task.call_args.kwargs["tenant_id"], "default")
        self.assertEqual(self.memory.reject_task.call_args.kwargs["tenant_id"], "default")

    def test_portfolio_mode_lists_roots_with_filters(self):
        self.memory.list_programs.return_value = {"programs": [], "total": 0}
        response = self.client.get("/admin/programs?portfolio=1&phase=implement&owner=alice&status=blocked&waiting_hours=4")
        self.assertEqual(response.status_code, 200)
        kwargs = self.memory.list_programs.call_args.kwargs
        self.assertTrue(kwargs["roots_only"])
        self.assertEqual((kwargs["phase"], kwargs["owner"], kwargs["status"]), ("implement", "alice", "blocked"))

    def test_portfolio_rejects_bad_waiting_hours(self):
        response = self.client.get("/admin/programs?portfolio=1&waiting_hours=soon")
        self.assertEqual(response.status_code, 400)


class PortfolioBffTests(unittest.TestCase):
    def test_rows_carry_wait_escalation_and_filter_by_age(self):
        memory = mock.MagicMock()
        memory.list_programs.return_value = {
            "programs": [
                {"id": "p-old", "requester_actor_id": "alice", "current_phase": "implement", "status": "running", "child_count": 2},
                {"id": "p-new", "requester_actor_id": "bob", "current_phase": "prd", "status": "running", "child_count": 0},
            ],
            "total": 2,
        }
        memory.get_program_status.side_effect = lambda pid: {
            "p-old": {
                "status": "blocked",
                "waiting_on": {"kind": "external", "event_type": "github.merge", "age_seconds": 30 * 3600, "escalated": True},
                "recommended_action": "review_and_merge_or_cancel_pr",
            },
            "p-new": {"status": "running", "waiting_on": {"kind": "approval", "age_seconds": 600}},
        }[pid]
        auth = mock.Mock(tenant_id="acme")
        rows = build_portfolio(memory, auth, waiting_hours=24)["programs"]
        self.assertEqual([row["id"] for row in rows], ["p-old"])
        row = rows[0]
        self.assertEqual(
            (row["owner"], row["phase"], row["status"], row["waiting_on_kind"], row["escalated"], row["child_count"]),
            ("alice", "implement", "blocked", "external", True, 2),
        )
        self.assertEqual(row["recommended_action"], "review_and_merge_or_cancel_pr")
        self.assertTrue(memory.list_programs.call_args.kwargs["roots_only"])


class GatewayPortfolioQueryTests(unittest.TestCase):
    def test_roots_phase_and_owner_filters_and_child_count(self):
        gateway = load_gateway()
        executed: list[tuple[str, Any]] = []
        cursor = mock.MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.execute.side_effect = lambda sql, params=None: executed.append((sql, params))
        cursor.fetchone.return_value = {"count": 1}
        cursor.fetchall.return_value = [{"id": "p-1", "child_count": 3, "metadata": {}}]
        conn = mock.MagicMock()
        conn.cursor.return_value = cursor
        result = gateway.list_delivery_programs(conn, tenant_id="acme", roots_only=True, phase="implement", owner="alice")
        sql, params = executed[-1]
        self.assertIn("parent_program_id IS NULL", sql)
        self.assertIn("current_phase = %s", sql)
        self.assertIn("requester_actor_id = %s", sql)
        self.assertEqual(params[:3], ("acme", "implement", "alice"))
        self.assertEqual(result["programs"][0]["child_count"], 3)


if __name__ == "__main__":
    unittest.main()
