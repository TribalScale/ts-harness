"""OIDC discovery caching and its use for endpoints and JWKS."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from ts_harness.config.schema import IdentityConfig
from ts_harness.gateway import oidc_discovery
from ts_harness.gateway.admin_bff import auth_config_payload
from ts_harness.gateway.identity import OidcIdentityProvider

DOC = {
    "issuer": "https://tenant.okta.example/oauth2/default",
    "authorization_endpoint": "https://tenant.okta.example/oauth2/default/v1/authorize",
    "token_endpoint": "https://tenant.okta.example/oauth2/default/v1/token",
    "jwks_uri": "https://tenant.okta.example/oauth2/default/v1/keys",
}


def _response(payload, status=200):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.side_effect = None if status < 400 else RuntimeError(f"HTTP {status}")
    return resp


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        oidc_discovery.clear_cache()
        self.addCleanup(oidc_discovery.clear_cache)

    def test_fetches_well_known_and_caches(self):
        client = mock.Mock()
        client.get.return_value = _response(DOC)
        self.assertEqual(oidc_discovery.discover(DOC["issuer"] + "/", client=client), DOC)
        oidc_discovery.discover(DOC["issuer"] + "/", client=client)
        client.get.assert_called_once()
        self.assertEqual(
            client.get.call_args.args[0], DOC["issuer"] + "/.well-known/openid-configuration"
        )

    def test_failure_returns_empty_and_is_cached_briefly(self):
        client = mock.Mock()
        client.get.return_value = _response({}, status=500)
        self.assertEqual(oidc_discovery.discover("https://down.example/", client=client), {})
        oidc_discovery.discover("https://down.example/", client=client)
        client.get.assert_called_once()

    def test_blank_or_placeholder_issuer_skips_network(self):
        client = mock.Mock()
        self.assertEqual(oidc_discovery.discover("", client=client), {})
        self.assertEqual(oidc_discovery.discover("{{OIDC_ISSUER}}", client=client), {})
        client.get.assert_not_called()


class DiscoveryUseTests(unittest.TestCase):
    def setUp(self):
        oidc_discovery.clear_cache()
        self.addCleanup(oidc_discovery.clear_cache)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("OIDC_AUTHORIZATION_ENDPOINT", "OIDC_TOKEN_ENDPOINT", "OIDC_CLIENT_ID"):
            os.environ.pop(key, None)

    def _runtime(self, **identity):
        return SimpleNamespace(config=SimpleNamespace(identity=IdentityConfig(provider="oidc", **identity)))

    def test_okta_preset_reads_endpoints_from_discovery(self):
        with mock.patch("ts_harness.gateway.oidc_discovery.discover", return_value=DOC):
            payload = auth_config_payload(self._runtime(preset="okta", issuer=DOC["issuer"], client_id="c"))
        self.assertEqual(payload["authorization_endpoint"], DOC["authorization_endpoint"])
        self.assertEqual(payload["token_endpoint"], DOC["token_endpoint"])
        self.assertEqual(payload["jwks_url"], DOC["jwks_uri"])
        self.assertEqual(payload["token_type"], "access")

    def test_okta_without_discovery_does_not_guess_auth0_paths(self):
        with mock.patch("ts_harness.gateway.oidc_discovery.discover", return_value={}):
            payload = auth_config_payload(self._runtime(preset="okta", issuer=DOC["issuer"]))
        self.assertEqual(payload["authorization_endpoint"], "")
        self.assertEqual(payload["token_endpoint"], "")

    def test_auth0_preset_keeps_default_paths_without_network(self):
        with mock.patch("ts_harness.gateway.oidc_discovery.discover") as discover:
            payload = auth_config_payload(self._runtime(preset="auth0", issuer="https://login.example.com/"))
        discover.assert_not_called()
        self.assertEqual(payload["authorization_endpoint"], "https://login.example.com/authorize")
        self.assertEqual(payload["token_endpoint"], "https://login.example.com/oauth/token")

    def test_google_preset_reports_id_token_type(self):
        with mock.patch("ts_harness.gateway.oidc_discovery.discover", return_value={}):
            payload = auth_config_payload(self._runtime(preset="google", issuer="https://accounts.google.com"))
        self.assertEqual(payload["token_type"], "id")

    def test_provider_uses_discovered_jwks_uri(self):
        provider = OidcIdentityProvider(IdentityConfig(provider="oidc", preset="okta", issuer=DOC["issuer"]))
        with mock.patch("ts_harness.gateway.identity.discover", return_value=DOC), mock.patch(
            "ts_harness.gateway.identity.httpx.get", return_value=_response({"keys": [{"kid": "k"}]})
        ) as get:
            self.assertEqual(provider._load_jwks(), [{"kid": "k"}])
        self.assertEqual(get.call_args.args[0], DOC["jwks_uri"])


if __name__ == "__main__":
    unittest.main()
