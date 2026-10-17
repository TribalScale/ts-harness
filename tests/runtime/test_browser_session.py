"""Console session lives in an HttpOnly cookie."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            gateway=SimpleNamespace(
                token="static-token",
                single_tenant_mode=True,
                default_tenant_id="default",
                allowed_origins=[],
            ),
            identity=SimpleNamespace(provider="static", service_permissions=None, roles={}),
        ),
        audit=SimpleNamespace(append=mock.Mock()),
    )


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _runtime()
        memory = mock.Mock()
        memory.get_program.return_value = {"program": {"tenant_id": "default"}}
        memory.cancel_program.return_value = {"status": "cancelled"}
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "get_memory_client", return_value=memory),
            mock.patch("ts_harness.gateway.admin_bff.build_inbox", return_value={"items": []}),
            mock.patch(
                "ts_harness.workflows.program_compensation.compensate_cancelled_program",
                return_value=None,
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def test_session_sets_an_httponly_cookie_and_hides_the_token(self):
        response = self.client.post("/admin/auth/session", json={"access_token": "static-token"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access_token", response.json())
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertNotIn("Secure", response.headers["set-cookie"])

    def test_inbox_accepts_the_cookie_without_a_bearer(self):
        response = self.client.get("/admin/inbox", headers={"cookie": "ec_session=static-token"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

    def test_cookie_mutations_require_a_same_origin(self):
        denied = self.client.post(
            "/admin/programs/p1/cancel",
            headers={"cookie": "ec_session=static-token", "origin": "https://evil.test"},
            json={"reason": "stop"},
        )
        self.assertEqual(denied.status_code, 403)

        accepted = self.client.post(
            "/admin/programs/p1/cancel",
            headers={"cookie": "ec_session=static-token", "origin": "http://testserver"},
            json={"reason": "stop"},
        )
        self.assertNotEqual(accepted.status_code, 403)

    def test_secure_follows_forwarded_https(self):
        response = self.client.post(
            "/admin/auth/session",
            json={"access_token": "static-token"},
            headers={"x-forwarded-proto": "https"},
        )

        self.assertIn("Secure", response.headers["set-cookie"])

    def test_logout_clears_the_cookie(self):
        response = self.client.post("/admin/auth/logout")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])


if __name__ == "__main__":
    unittest.main()
