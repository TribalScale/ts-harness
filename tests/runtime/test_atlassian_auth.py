"""AtlassianAuth: service account OAuth, basic fallback, token cache and cloud id discovery."""

from __future__ import annotations

import base64
import json
import unittest
from typing import Any
from unittest import mock

from ts_harness.integrations.atlassian_auth import TOKEN_URL, AtlassianAuth, AtlassianAuthError

OAUTH_ENV = {
    "ATLASSIAN_CLIENT_ID": "client-id",
    "ATLASSIAN_CLIENT_SECRET": "client-secret-value",
    "ATLASSIAN_SITE_URL": "acme.atlassian.net",
}
BASIC_ENV = {
    "ATLASSIAN_SITE_URL": "https://acme.atlassian.net/",
    "ATLASSIAN_EMAIL": "bot@acme.io",
    "ATLASSIAN_API_TOKEN": "api-token",
}


class FakeTransport:
    def __init__(self, token_status: int = 200, expires_in: int = 3600, tenant: dict[str, Any] | None = None):
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.token_status = token_status
        self.expires_in = expires_in
        self.tenant = tenant if tenant is not None else {"cloudId": "cloud-123"}
        self.issued = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, json.loads(body) if body else None))
        if url == TOKEN_URL:
            if self.token_status >= 400:
                return self.token_status, json.dumps({"error": "access_denied"}).encode(), {}
            self.issued += 1
            return 200, json.dumps({"access_token": f"tok-{self.issued}", "expires_in": self.expires_in}).encode(), {}
        if url.endswith("/_edge/tenant_info"):
            return 200, json.dumps(self.tenant).encode(), {}
        return 404, b"", {}


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _auth(env: dict[str, str], transport: FakeTransport, clock: Clock | None = None) -> AtlassianAuth:
    with mock.patch.dict("os.environ", env, clear=True):
        return AtlassianAuth.from_env(transport=transport, clock=clock)


class AtlassianAuthModeTests(unittest.TestCase):
    def test_oauth_preferred_when_client_credentials_present(self):
        auth = _auth({**BASIC_ENV, **OAUTH_ENV}, FakeTransport())
        self.assertEqual(auth.mode, "oauth")

    def test_basic_mode_uses_site_and_email_token(self):
        transport = FakeTransport()
        auth = _auth(BASIC_ENV, transport)
        self.assertEqual(auth.mode, "basic")
        self.assertEqual(auth.base_url(), "https://acme.atlassian.net")
        expected = "Basic " + base64.b64encode(b"bot@acme.io:api-token").decode()
        self.assertEqual(auth.authorization(), expected)
        self.assertEqual(transport.calls, [])

    def test_legacy_jira_names_still_work(self):
        auth = _auth({"JIRA_URL": "acme.atlassian.net", "JIRA_USERNAME": "bot@acme.io", "JIRA_API_TOKEN": "t"}, FakeTransport())
        self.assertEqual(auth.mode, "basic")
        self.assertEqual(auth.base_url(), "https://acme.atlassian.net")

    def test_missing_credentials_names_both_modes(self):
        with self.assertRaises(AtlassianAuthError) as ctx:
            _auth({}, FakeTransport())
        self.assertIn("ATLASSIAN_CLIENT_ID", str(ctx.exception))
        self.assertIn("ATLASSIAN_API_TOKEN", str(ctx.exception))

    def test_oauth_requires_cloud_id_or_site(self):
        with self.assertRaises(AtlassianAuthError):
            _auth({"ATLASSIAN_CLIENT_ID": "a", "ATLASSIAN_CLIENT_SECRET": "b"}, FakeTransport())


class AtlassianOAuthTests(unittest.TestCase):
    def test_token_request_and_gateway_base_url(self):
        transport = FakeTransport()
        auth = _auth(OAUTH_ENV, transport)
        self.assertEqual(auth.authorization(), "Bearer tok-1")
        self.assertEqual(auth.base_url(), "https://api.atlassian.com/ex/jira/cloud-123")
        method, url, body = transport.calls[0]
        self.assertEqual((method, url), ("POST", TOKEN_URL))
        self.assertEqual(body, {"grant_type": "client_credentials", "client_id": "client-id", "client_secret": "client-secret-value"})
        self.assertEqual(transport.calls[1][:2], ("GET", "https://acme.atlassian.net/_edge/tenant_info"))

    def test_token_cached_until_refresh_margin(self):
        transport = FakeTransport(expires_in=3600)
        clock = Clock()
        auth = _auth(OAUTH_ENV, transport, clock)
        self.assertEqual(auth.authorization(), "Bearer tok-1")
        clock.now += 3500
        self.assertEqual(auth.authorization(), "Bearer tok-1")
        clock.now += 50
        self.assertEqual(auth.authorization(), "Bearer tok-2")

    def test_invalidate_forces_new_token(self):
        transport = FakeTransport()
        auth = _auth(OAUTH_ENV, transport)
        auth.authorization()
        auth.invalidate()
        self.assertEqual(auth.authorization(), "Bearer tok-2")

    def test_cloud_id_from_env_skips_discovery(self):
        transport = FakeTransport()
        auth = _auth({**OAUTH_ENV, "ATLASSIAN_CLOUD_ID": "env-cloud"}, transport)
        self.assertEqual(auth.base_url(), "https://api.atlassian.com/ex/jira/env-cloud")
        self.assertEqual(transport.calls, [])

    def test_cloud_id_discovered_once(self):
        transport = FakeTransport()
        auth = _auth(OAUTH_ENV, transport)
        auth.base_url()
        auth.base_url()
        self.assertEqual(sum(1 for c in transport.calls if c[1].endswith("tenant_info")), 1)

    def test_failed_discovery_raises(self):
        auth = _auth(OAUTH_ENV, FakeTransport(tenant={}))
        with self.assertRaises(AtlassianAuthError):
            auth.base_url()

    def test_token_failure_never_leaks_secret(self):
        auth = _auth(OAUTH_ENV, FakeTransport(token_status=401))
        with self.assertRaises(AtlassianAuthError) as ctx:
            auth.authorization()
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn("client-secret-value", str(ctx.exception))
        self.assertNotIn("client-secret-value", repr(auth))


if __name__ == "__main__":
    unittest.main()
