"""Admin program advance must materialize via DeliveryProgramService."""

from __future__ import annotations

import unittest
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app


class AdminProgramsAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.runtime.config.gateway.cors_origins = ["*"]
        self.runtime.audit.append = mock.Mock()
        self.auth = mock.Mock(
            actor_id="admin-1",
            roles=["admin"],
            tenant_id="default",
        )
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_advance_routes_through_delivery_program_service(self):
        service = mock.Mock()
        service.advance_program.return_value = {
            "program_id": "prog-1",
            "target_phase": "jira",
            "workflow_id": "wf-jira",
            "workflow_type": "jira_from_prd",
            "task_ids": ["t1"],
            "first_step": "create_jira",
        }
        memory = mock.Mock()
        memory.get_program.return_value = {"program": {"id": "prog-1", "tenant_id": "default"}}

        with mock.patch.object(
            asgi_app,
            "get_delivery_program_service",
            return_value=service,
        ), mock.patch.object(
            asgi_app,
            "get_memory_client",
            return_value=memory,
        ):
            response = self.client.post(
                "/admin/programs/prog-1/advance",
                json={"target_phase": "jira", "summary": "Operator advance"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workflow_id"], "wf-jira")
        self.assertEqual(body["target_phase"], "jira")
        service.advance_program.assert_called_once()
        kwargs = service.advance_program.call_args.kwargs
        self.assertEqual(kwargs["program_id"], "prog-1")
        self.assertEqual(kwargs["target_phase"], "jira")
        self.assertEqual(kwargs["summary"], "Operator advance")
        self.assertEqual(kwargs["actor"], "admin-1")
        memory.advance_program.assert_not_called()

    def test_advance_returns_400_on_invalid_edge(self):
        service = mock.Mock()
        service.advance_program.side_effect = ValueError(
            "cannot advance from prd to deploy; allowed: jira, work_orders"
        )

        memory = mock.Mock()
        memory.get_program.return_value = {"program": {"id": "prog-1", "tenant_id": "default"}}

        with mock.patch.object(
            asgi_app,
            "get_delivery_program_service",
            return_value=service,
        ), mock.patch.object(asgi_app, "get_memory_client", return_value=memory):
            response = self.client.post(
                "/admin/programs/prog-1/advance",
                json={"target_phase": "deploy"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot advance", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
