"""Routes that previously checked only the bearer now check a permission."""

from __future__ import annotations

import json
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.server import GatewayHandler


def _runtime() -> mock.Mock:
    runtime = mock.Mock()
    runtime.config = SimpleNamespace(identity=SimpleNamespace(role_bundles={}, service_permissions=None))
    runtime.audit.append = mock.Mock()
    runtime.scheduler.run_due_once = mock.Mock()
    runtime.handle_workflow_present_artifact = mock.Mock(return_value={"status": "ok"})
    return runtime


class RoutePermissionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _runtime()
        self.auth = AuthContext(actor_id="ada", tenant_id="default", roles=["member"], auth_provider="oidc")
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", side_effect=lambda request, body: self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch("ts_harness.gateway.admin_bff.build_inbox", return_value={"items": []}),
            mock.patch("ts_harness.gateway.admin_bff.list_sessions_admin", return_value={"sessions": []}),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_member_cannot_trigger_cron_present_or_patch(self):
        cron = self.client.post("/cron/trigger", json={})
        present = self.client.post("/workflow/present-artifact", json={"workflow_id": "w", "artifact": {"a": 1}})
        patch = self.client.patch("/admin/programs/p1", json={"project_id": "proj"})
        self.assertEqual(cron.status_code, 403)
        self.assertEqual(present.status_code, 403)
        self.assertEqual(patch.status_code, 403)
        self.runtime.scheduler.run_due_once.assert_not_called()

    def test_role_without_view_cannot_read_operator_routes(self):
        self.auth = AuthContext(actor_id="ada", tenant_id="default", roles=["nobody"], auth_provider="oidc")
        paths = (
            "/admin/inbox",
            "/admin/sessions",
            "/admin/sessions/search?q=hi",
            "/admin/sessions/s1/messages",
            "/admin/approvals",
            "/admin/approvals/a1",
            "/admin/programs",
            "/admin/programs/p1",
            "/admin/programs/p1/status",
            "/admin/workflows/w1",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403, path)

    def test_viewer_can_read_inbox_and_sessions(self):
        self.auth = AuthContext(actor_id="ada", tenant_id="default", roles=["viewer"], auth_provider="oidc")
        self.assertEqual(self.client.get("/admin/inbox").status_code, 200)
        self.assertEqual(self.client.get("/admin/sessions").status_code, 200)

    def test_static_token_can_trigger_cron(self):
        self.auth = AuthContext(actor_id="svc", tenant_id="default", roles=[], auth_provider="static")
        response = self.client.post("/cron/trigger", json={})
        self.assertEqual(response.status_code, 200)
        self.runtime.scheduler.run_due_once.assert_called_once()

    def test_viewer_can_read_a_program_and_start_uses_the_node_permission(self):
        self.auth = AuthContext(actor_id="ada", tenant_id="default", roles=["viewer"], auth_provider="oidc")
        memory = mock.Mock()
        memory.get_program.return_value = {
            "program": {"id": "p1", "tenant_id": "default", "program_type": "software_delivery"}
        }
        with mock.patch.object(asgi_app, "get_memory_client", return_value=memory):
            self.assertEqual(self.client.get("/admin/programs/p1").status_code, 200)

        self.auth = AuthContext(actor_id="ada", tenant_id="default", roles=["requester"], auth_provider="oidc")
        with mock.patch(
            "ts_harness.gateway.program_definitions_bff.program_action_permission",
            return_value="start_delivery",
        ):
            denied = self.client.post(
                "/admin/program-definitions/software_delivery/start",
                json={"summary": "ship", "entry_phase": "implement"},
            )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"], "missing permission")

        with mock.patch(
            "ts_harness.gateway.program_definitions_bff.program_action_permission",
            return_value="start_program",
        ), mock.patch(
            "ts_harness.gateway.program_definitions_bff.start_program_definition_admin",
            return_value={"status": "started"},
        ), mock.patch.object(asgi_app, "get_delivery_program_service", return_value=mock.Mock()), mock.patch.object(
            asgi_app, "get_program_definition_registry", return_value=mock.Mock(reload_if_changed=lambda: True)
        ):
            allowed = self.client.post(
                "/admin/program-definitions/software_delivery/start",
                json={"summary": "ship", "entry_phase": "prd"},
            )
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_promote_requires_a_matching_department(self):
        self.auth = AuthContext(
            actor_id="ada",
            tenant_id="default",
            roles=["member"],
            email="ada@example.com",
            auth_provider="oidc",
        )
        denied = self.client.post(
            "/admin/programs/p1/department-memory",
            json={"brief_id": "00000000-0000-4000-8000-000000000011"},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"], "missing permission")

        self.auth = AuthContext(
            actor_id="ada",
            tenant_id="default",
            roles=["admin"],
            email="ada@example.com",
            auth_provider="oidc",
        )
        self.runtime.config.identity.departments = {"ada@example.com": "engineering"}
        memory = mock.Mock()
        memory.get_program.return_value = {
            "program": {"id": "p1", "tenant_id": "default", "department": "sales"}
        }
        with mock.patch.object(asgi_app, "get_memory_client", return_value=memory):
            mismatched = self.client.post(
                "/admin/programs/p1/department-memory",
                json={"brief_id": "00000000-0000-4000-8000-000000000011"},
            )
        self.assertEqual(mismatched.status_code, 403)
        self.assertEqual(mismatched.json()["error"], "department mismatch")
        self.assertNotIn("sales", mismatched.json()["error"])
        memory.promote_department_memory.assert_not_called()

        memory.get_program.return_value = {
            "program": {"id": "p1", "tenant_id": "default", "department": "engineering"}
        }
        memory.promote_department_memory.return_value = {"id": "m1", "action": "created"}
        with mock.patch.object(asgi_app, "get_memory_client", return_value=memory):
            allowed = self.client.post(
                "/admin/programs/p1/department-memory",
                json={"brief_id": "00000000-0000-4000-8000-000000000011"},
            )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        memory.promote_department_memory.assert_called_once()

    def test_console_start_copies_the_starter_department(self):
        from ts_harness.gateway.program_definitions_bff import start_program_definition_admin

        runtime = SimpleNamespace(
            config=SimpleNamespace(
                identity=SimpleNamespace(departments={"ada@example.com": "engineering"})
            )
        )
        auth = AuthContext(
            actor_id="ada",
            tenant_id="default",
            roles=["admin"],
            email="ada@example.com",
            auth_provider="oidc",
        )
        service = mock.Mock()
        service.create_program.return_value = SimpleNamespace(
            program_id="p1",
            workflow_id="w",
            workflow_type="prd_request",
            entry_phase="prd",
            task_ids=[],
            first_step=None,
        )
        with mock.patch(
            "ts_harness.gateway.program_definitions_bff._resolve_program_definition",
            return_value={"startable_nodes": ["prd"]},
        ):
            start_program_definition_admin(
                runtime,
                auth,
                "software_delivery",
                {"summary": "ship", "entry_phase": "prd", "department": "sales"},
                service,
            )
        self.assertEqual(service.create_program.call_args.kwargs["department"], "engineering")


class StdlibCronPermissionTests(unittest.TestCase):
    def test_member_is_denied(self):
        runtime = _runtime()
        handler = GatewayHandler.__new__(GatewayHandler)
        body = b"{}"
        handler.path = "/cron/trigger"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        sent: dict = {}
        handler._send = lambda status, payload: sent.update(status=status, payload=payload)
        handler._check_origin = lambda: None
        handler._authenticate = lambda request_body: AuthContext(
            actor_id="ada", tenant_id="default", roles=["member"], auth_provider="oidc"
        )
        with mock.patch("ts_harness.gateway.server.get_runtime", return_value=runtime):
            handler.do_POST()
        self.assertEqual(sent["status"], 403)
        self.assertEqual(sent["payload"]["error"], "missing permission")
        runtime.scheduler.run_due_once.assert_not_called()
        self.assertTrue(json.dumps(sent))
