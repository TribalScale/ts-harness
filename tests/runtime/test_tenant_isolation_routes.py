"""Cross-tenant requests look like missing records; missing permissions get 403.

Regression tests for docs/security/tenant-isolation-review.md, plus a contract test
that every gateway route authenticates and checks a permission unless it is listed as
deliberately open.
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app
from ts_harness.gateway.auth import AuthContext
from ts_harness.tools.approvals import ApprovalQueue


def _runtime() -> mock.Mock:
    runtime = mock.Mock()
    runtime.config = SimpleNamespace(
        identity=SimpleNamespace(role_bundles={}, service_permissions=None, provider="static"),
        gateway=SimpleNamespace(default_tenant_id="default", allowed_origins=[]),
    )
    runtime.audit.append = mock.Mock()
    return runtime


class _RouteCase(unittest.TestCase):
    roles: list[str] = ["admin"]
    provider = "oidc"

    def setUp(self):
        self.runtime = _runtime()
        self.auth = AuthContext(actor_id="ada", tenant_id="acme", roles=list(self.roles), auth_provider=self.provider)
        self.memory = mock.MagicMock()
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", side_effect=lambda request, body: self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()


class ToolApprovalTenantTests(_RouteCase):
    def setUp(self):
        super().setUp()
        self.queue = ApprovalQueue()
        mine = self.queue.request(tenant_id="acme", agent_id="a", tool_name="gmail.send", args={}, reason="r")
        theirs = self.queue.request(tenant_id="globex", agent_id="a", tool_name="gmail.send", args={"to": "x"}, reason="r")
        self.mine, self.theirs = mine.approval_id, theirs.approval_id
        patch = mock.patch.object(asgi_app, "get_approval_queue", return_value=self.queue)
        patch.start()
        self.patches.append(patch)

    def test_list_shows_only_the_callers_tenant(self):
        ids = [item["approval_id"] for item in self.client.get("/admin/approvals").json()["approvals"]]
        self.assertEqual(ids, [self.mine])

    def test_another_tenants_approval_is_not_found(self):
        self.assertEqual(self.client.get(f"/admin/approvals/{self.theirs}").status_code, 404)
        self.assertEqual(self.client.get(f"/admin/approvals/{self.mine}").status_code, 200)

    def test_another_tenants_approval_cannot_be_decided(self):
        with mock.patch.object(asgi_app, "decide_and_resume") as decide:
            response = self.client.post(f"/admin/approvals/{self.theirs}/decide", json={"approved": True})
        self.assertEqual(response.status_code, 404)
        decide.assert_not_called()
        self.assertEqual(self.queue.get(self.theirs).status, "pending")

    def test_inbox_lists_only_the_callers_tool_approvals(self):
        from ts_harness.gateway import admin_bff

        memory = mock.Mock()
        memory.list_programs.return_value = {"programs": []}
        memory.list_stuck_tasks.return_value = []
        with mock.patch.object(admin_bff, "get_approval_queue", return_value=self.queue), mock.patch(
            "ts_harness.skills.repository.skill_repository"
        ) as repo:
            repo.return_value.list_drafts.return_value = []
            inbox = admin_bff.build_inbox(self.runtime, self.auth, memory=memory, skills_root="/nonexistent")
        ids = {item["id"] for item in inbox["items"] if item.get("type") == "tool_approval"}
        self.assertEqual(ids, {self.mine})


class WorkflowGateTenantTests(_RouteCase):
    def _task_in(self, tenant: str) -> None:
        self.memory.task_program.return_value = {"task_id": "t1", "program_id": None, "workflow_id": "wf-1"}
        self.memory.get_workflow.return_value = {"workflow": {"id": "wf-1", "tenant_id": tenant}, "tasks": []}
        self.memory.get_workflow_tasks.return_value = {"tasks": []}

    def test_approve_and_reject_of_another_tenants_gate_are_not_found(self):
        self._task_in("globex")
        engine = mock.Mock()
        with mock.patch.object(asgi_app, "get_workflow_engine", return_value=engine):
            approve = self.client.post("/admin/workflows/approve", json={"id": "t1"})
            reject = self.client.post("/admin/workflows/reject", json={"id": "t1"})
        self.assertEqual((approve.status_code, reject.status_code), (404, 404))
        self.memory.approve_task.assert_not_called()
        self.memory.reject_task.assert_not_called()
        engine.handle_approve.assert_not_called()
        engine.handle_reject.assert_not_called()

    def test_service_token_is_held_to_its_tenant_too(self):
        self.auth = AuthContext(actor_id="svc", tenant_id="acme", auth_provider="static", is_service=True)
        self._task_in("globex")
        self.assertEqual(self.client.post("/admin/workflows/approve", json={"id": "t1"}).status_code, 404)
        self.memory.approve_task.assert_not_called()

    def test_own_tenant_gate_is_approved(self):
        self._task_in("acme")
        self.memory.approve_task.return_value = {"id": "t1", "approval_status": "approved"}
        with mock.patch("ts_harness.workflows.engine.hook_engine_enabled", return_value=False):
            response = self.client.post("/admin/workflows/approve", json={"id": "t1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.memory.approve_task.call_args.kwargs["tenant_id"], "acme")

    def test_workflow_of_an_unreadable_program_is_hidden(self):
        self.memory.get_workflow_tasks.return_value = {"tasks": [{"id": "t1", "tenant_id": "acme"}]}
        self.memory.task_program.return_value = {"task_id": "t1", "program_id": "p-private", "workflow_id": "wf-1"}
        self.memory.program_access_level.return_value = {"found": True, "tenant_id": "acme", "level": "none"}
        self.auth = AuthContext(actor_id="ada", tenant_id="acme", roles=["viewer"], auth_provider="oidc")
        with mock.patch.dict("os.environ", {"TS_HARNESS_PROGRAM_ACCESS": "enforce"}):
            self.assertEqual(self.client.get("/admin/workflows/wf-1").status_code, 404)
        self.memory.program_access_level.return_value = {"found": True, "tenant_id": "acme", "level": "read"}
        with mock.patch.dict("os.environ", {"TS_HARNESS_PROGRAM_ACCESS": "enforce"}):
            self.assertEqual(self.client.get("/admin/workflows/wf-1").status_code, 200)


class PermissionGapTests(_RouteCase):
    roles = ["viewer"]

    def test_viewer_cannot_delete_a_fragment_draft(self):
        with mock.patch("ts_harness.gateway.workflow_fragments_bff.delete_workflow_fragment_draft_admin") as delete:
            response = self.client.delete("/admin/workflow-fragments/drafts/d1")
        self.assertEqual(response.status_code, 403)
        delete.assert_not_called()

    def test_viewer_can_still_read_fragments(self):
        with mock.patch(
            "ts_harness.gateway.workflow_fragments_bff.list_workflow_fragments_admin", return_value={"fragments": []}
        ):
            self.assertEqual(self.client.get("/admin/workflow-fragments").status_code, 200)

    def test_viewer_cannot_send_a_test_message_as_the_bot(self):
        router = mock.Mock()
        with mock.patch.object(asgi_app, "get_channel_router", return_value=router):
            response = self.client.post("/channels/slack/send-test", json={"channel": "#general", "text": "hi"})
        self.assertEqual(response.status_code, 403)
        router.send.assert_not_called()

    def test_viewer_cannot_post_an_unsigned_github_delivery(self):
        with mock.patch("ts_harness.gateway.github_webhook.webhook_secrets", return_value=[]), mock.patch(
            "ts_harness.gateway.github_webhook.handle_github_webhook"
        ) as handle:
            response = self.client.post(
                "/webhooks/github", json={"action": "closed"}, headers={"x-github-event": "pull_request"}
            )
        self.assertEqual(response.status_code, 403)
        handle.assert_not_called()

    def test_skills_doctor_ignores_a_tenant_query_parameter(self):
        self.runtime.skill_catalog.doctor.return_value = []
        self.runtime.config.get_agent = lambda agent_id: SimpleNamespace(id=agent_id)
        response = self.client.get("/skills/doctor?tenant_id=globex")
        self.assertEqual(response.json()["tenant_id"], "acme")
        self.assertEqual(self.runtime.skill_catalog.doctor.call_args.kwargs["tenant_id"], "acme")


class GoogleOAuthTests(_RouteCase):
    roles = ["member"]

    def setUp(self):
        super().setUp()
        self.auth = AuthContext(
            actor_id="ada", tenant_id="acme", email="ada@example.com", roles=["member"], auth_provider="oidc"
        )
        env = mock.patch.dict(
            "os.environ",
            {
                "GOOGLE_OAUTH_CLIENT_ID": "cid",
                "GOOGLE_OAUTH_CLIENT_SECRET": "secret",
                "GOOGLE_OAUTH_CALLBACK_URL": "https://example.com/oauth/google/callback",
            },
        )
        env.start()
        self.patches.append(env)

    def test_start_binds_the_callers_tenant_and_own_email_only(self):
        from urllib.parse import parse_qs, urlparse

        from ts_harness.connectors.google_workspace.auth import oauth_flow

        other = self.client.get("/oauth/google/start?email=victim@example.com&tenant_id=globex", follow_redirects=False)
        self.assertEqual(other.status_code, 403)
        own = self.client.get("/oauth/google/start?tenant_id=globex", follow_redirects=False)
        self.assertEqual(own.status_code, 307)
        state = parse_qs(urlparse(own.headers["location"]).query)["state"][0]
        self.assertEqual(oauth_flow.parse_state(state)["tenant_id"], "acme")
        self.assertEqual(oauth_flow.parse_state(state)["email"], "ada@example.com")

    def test_callback_escapes_the_error_parameter(self):
        response = self.client.get("/oauth/google/callback?error=<script>alert(1)</script>")
        self.assertNotIn("<script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_callback_refuses_an_unsigned_state(self):
        import base64
        import json

        forged = base64.urlsafe_b64encode(json.dumps({"tenant_id": "globex", "email": "v@example.com"}).encode()).decode()
        with mock.patch(
            "ts_harness.connectors.google_workspace.auth.oauth_store.save_oauth_credentials"
        ) as save, mock.patch("ts_harness.connectors.google_workspace.auth.oauth_flow.exchange_code") as exchange:
            response = self.client.get(f"/oauth/google/callback?code=c&state={forged}")
        self.assertGreaterEqual(response.status_code, 400)
        exchange.assert_not_called()
        save.assert_not_called()


class KnowledgeNoteTenantTests(unittest.TestCase):
    def setUp(self):
        self.auth = AuthContext(actor_id="ada", tenant_id="acme", roles=["admin"], auth_provider="oidc")
        self.memory = mock.Mock()
        self.memory.get_project.return_value = {"project": {"id": "proj-a", "tenant_id": "acme", "team_slug": "web"}}

    def test_promote_refuses_a_note_of_another_project_or_tenant(self):
        from ts_harness.gateway.knowledge_bff import promote_project_note

        for note in (
            {"id": "n1", "scope": "project", "project_id": "proj-b", "tenant_id": "acme"},
            {"id": "n1", "scope": "project", "project_id": "proj-a", "tenant_id": "globex"},
            {"id": "n1", "scope": "org", "tenant_id": "acme"},
        ):
            with self.subTest(note=note):
                self.memory.get_knowledge_note.return_value = note
                with mock.patch("ts_harness.gateway.knowledge_bff._project", return_value={"id": "proj-a"}):
                    with self.assertRaises(LookupError):
                        promote_project_note(self.memory, self.auth, None, "proj-a", "n1")
        self.memory.promote_knowledge_note.assert_not_called()

    def test_versions_of_another_tenants_org_note_are_not_found(self):
        from ts_harness.gateway.knowledge_bff import note_versions_admin

        self.memory.get_knowledge_note.return_value = {"id": "n1", "scope": "org", "tenant_id": "globex"}
        with self.assertRaises(LookupError):
            note_versions_admin(self.memory, self.auth, None, "n1")
        self.memory.knowledge_note_versions.assert_not_called()


class AgentWorkflowToolTenantTests(unittest.TestCase):
    def test_a_model_named_workflow_of_another_tenant_is_not_found(self):
        from ts_harness.tools import workflow_tools
        from ts_harness.tools.policy import ToolPolicyContext

        session = SimpleNamespace(session_id="s1", tenant_id="acme", metadata={"workflow_id": "wf-own"})
        memory = mock.Mock()
        memory.get_workflow.return_value = {"workflow": {"id": "wf-other", "tenant_id": "globex"}}
        engine = mock.Mock()
        ctx = ToolPolicyContext(tenant_id="acme", agent_id="orchestrator", source="web", session_id="s1")
        with mock.patch.object(workflow_tools, "get_session_store", return_value=mock.Mock(get=lambda _id: session)), \
                mock.patch.object(workflow_tools, "get_memory_client", return_value=memory), \
                mock.patch.object(workflow_tools, "get_workflow_engine", return_value=engine):
            status = workflow_tools.workflow_status_handler({"workflow_id": "wf-other"}, ctx)
            cancel = workflow_tools.cancel_workflow_handler({"workflow_id": "wf-other"}, ctx)
            resume = workflow_tools.resume_external_event_handler(
                {"workflow_id": "wf-other", "event_type": "github.merge"}, ctx
            )
            own = workflow_tools.workflow_status_handler({}, ctx)
        for result in (status, cancel, resume):
            self.assertEqual(result, {"error": "workflow not found"})
        engine.cancel.assert_not_called()
        engine.resume_external.assert_not_called()
        engine.get_status.assert_called_once_with("wf-own")
        self.assertIsNotNone(own)


# ── Contract: every route authenticates and checks a permission ──────────────

# Calls that authenticate a request (directly or inside a shared helper).
_AUTH_CALLS = ("_auth(", "_share_gate(", "_admin_projects_call(", "_admin_fragment_call(")
# Calls that check a permission (the helpers above call _require_admin themselves).
_PERMISSION_CALLS = ("_require_admin(", "_share_gate(", "_admin_projects_call(", "_admin_fragment_call(")

# Routes that are deliberately open or authenticate some other way. Adding a route here
# needs a reason in docs/security/tenant-isolation-review.md.
_OPEN_ROUTES = {
    "/healthz": "liveness probe",
    "/metrics": "Prometheus scrape (network-restricted)",
    "/oauth/google/callback": "OAuth redirect; the state is HMAC-signed by /oauth/google/start",
    "/admin/auth/session": "exchanges an IdP token for the session cookie",
    "/admin/auth/logout": "clears the session cookie",
    "/admin/auth/config": "sign-in configuration for the login page",
    "/internal/coding-jobs/{job_id}/github-token": "job token verified by the memory API for that job only",
    "/scim/v2/ServiceProviderConfig": "SCIM bearer token (_scim_denied)",
    "/scim/v2/{resource:str}": "SCIM bearer token (_scim_denied)",
    "/scim/v2/{resource:str}/{id:str}": "SCIM bearer token (_scim_denied)",
}
# Routes any authenticated caller may use: chat and dispatch go through tool policy.
_AUTH_ONLY_ROUTES = {
    "/oauth/google/start",
    "/skills/doctor",
    "/mcp/tools",
    "/connectors/google-workspace/tools",
    "/memory/smoke",
    "/workflow/dispatch",
    "/workflow/cancel",
    "/session/message",
    "/intake/uploads",
    "/sandbox/deny-check",
}


class RouteAuthContractTests(unittest.TestCase):
    def _routes(self) -> list[Route]:
        return [route for route in asgi_app.routes if isinstance(route, Route)]

    def test_every_route_authenticates_and_checks_a_permission(self):
        problems = []
        for route in self._routes():
            if route.path in _OPEN_ROUTES:
                continue
            source = inspect.getsource(route.endpoint)
            if not any(call in source for call in _AUTH_CALLS):
                problems.append(f"{route.path}: no authentication")
                continue
            if route.path in _AUTH_ONLY_ROUTES:
                continue
            if not any(call in source for call in _PERMISSION_CALLS):
                problems.append(f"{route.path}: no permission check")
        self.assertEqual(problems, [])

    def test_open_routes_are_still_routes(self):
        paths = {route.path for route in self._routes()}
        self.assertEqual(set(_OPEN_ROUTES) - paths, set())
        self.assertEqual(_AUTH_ONLY_ROUTES - paths, set())

    def test_program_routes_scope_the_program_by_tenant(self):
        tenant_checks = ("_program_for_tenant(", "_program_mutation_gate(", "_share_gate(", "program_budget_admin")
        problems = []
        for route in self._routes():
            if not route.path.startswith(("/admin/programs/{id}", "/admin/budgets/programs/")):
                continue
            source = inspect.getsource(route.endpoint)
            if not any(call in source for call in tenant_checks):
                problems.append(route.path)
        self.assertEqual(problems, [])

    def test_channel_mount_authenticates(self):
        mounts = [route for route in asgi_app.routes if isinstance(route, Mount)]
        for mount in mounts:
            for route in mount.routes:
                source = inspect.getsource(route.endpoint)
                self.assertIn("_auth_channel(", source)
                self.assertIn("_require_admin(", source)  # send-test


if __name__ == "__main__":
    unittest.main()
