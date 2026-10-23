"""POST /workflow/dispatch reports runtime failures as a blocked dispatch."""

from __future__ import annotations

import unittest
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app


class WorkflowDispatchErrorTests(unittest.TestCase):
    def test_runtime_error_returns_blocked_result(self):
        runtime = mock.Mock()
        runtime.dispatch_workflow.side_effect = RuntimeError("boom")
        with (
            mock.patch.object(asgi_app, "get_runtime", return_value=runtime),
            mock.patch.object(asgi_app, "_check_origin"),
            mock.patch.object(asgi_app, "_auth", return_value=mock.Mock()),
        ):
            response = TestClient(asgi_app.app).post("/workflow/dispatch", json={"agent_id": "dev-agent"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["status"], "blocked")
        self.assertEqual(response.json()["error"], "boom")
