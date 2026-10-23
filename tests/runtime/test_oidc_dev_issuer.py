"""End-to-end PKCE sign-in against the local dev issuer."""

from __future__ import annotations

import importlib.util
import secrets
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from ts_harness.config.schema import IdentityConfig
from ts_harness.gateway.identity import OidcIdentityProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dev" / "oidc-dev-issuer.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("oidc_dev_issuer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DevIssuerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()
        cls.server = cls.mod.serve("http://127.0.0.1:0/", 0, host="127.0.0.1")
        port = cls.server.server_address[1]
        cls.issuer = f"http://127.0.0.1:{port}/"
        cls.server.issuer.issuer = cls.issuer
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _sign_in(self, verifier: str, *, challenge: str | None = None, groups: str = "ts-harness-users") -> httpx.Response:
        redirect = "http://localhost:8080/"
        form = {
            "client_id": "ts-harness-admin",
            "redirect_uri": redirect,
            "state": "s1",
            "code_challenge": challenge or self.mod.pkce_s256(verifier),
            "code_challenge_method": "S256",
            "audience": "https://ts-harness/api",
            "scope": "openid profile email",
            "sub": "dev|bob",
            "email": "bob@example.com",
            "name": "Bob",
            "groups": groups,
        }
        resp = httpx.post(self.issuer + "authorize", data=form, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        location = urlparse(resp.headers["location"])
        query = parse_qs(location.query)
        self.assertEqual(query["state"], ["s1"])
        return httpx.post(
            self.issuer + "oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "code_verifier": verifier,
                "redirect_uri": redirect,
                "client_id": "ts-harness-admin",
            },
        )

    def test_discovery_points_at_its_own_endpoints(self):
        doc = httpx.get(self.issuer + ".well-known/openid-configuration").json()
        self.assertEqual(doc["issuer"], self.issuer)
        self.assertEqual(doc["jwks_uri"], self.issuer + ".well-known/jwks.json")

    def test_authorize_form_renders(self):
        resp = httpx.get(
            self.issuer + "authorize",
            params={"response_type": "code", "code_challenge_method": "S256", "client_id": "c"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Dev sign-in", resp.text)

    def test_pkce_flow_issues_tokens_the_runtime_accepts(self):
        verifier = secrets.token_urlsafe(32)
        resp = self._sign_in(verifier, groups="ts-harness-users, ts-harness-team-payments")
        self.assertEqual(resp.status_code, 200, resp.text)
        tokens = resp.json()

        provider = OidcIdentityProvider(
            IdentityConfig(
                provider="oidc",
                preset="auth0",
                issuer=self.issuer,
                audience="https://ts-harness/api",
                client_id="ts-harness-admin",
                jwks_url=self.issuer + ".well-known/jwks.json",
                allowed_domains=["example.com"],
            )
        )
        identity = provider.validate_token(tokens["access_token"])
        self.assertEqual(identity["actor_id"], "dev|bob")
        self.assertEqual(identity["groups"], ["ts-harness-users", "ts-harness-team-payments"])

    def test_wrong_verifier_is_rejected(self):
        resp = self._sign_in("wrong-verifier", challenge=self.mod.pkce_s256("right-verifier"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_grant")


if __name__ == "__main__":
    unittest.main()
