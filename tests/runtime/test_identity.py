"""Tests for OIDC JWT validation and SAML stub in gateway/identity.py."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from ts_harness.config.schema import IdentityConfig
from ts_harness.gateway.identity import (
    OidcIdentityProvider,
    SamlIdentityProvider,
    StaticIdentityProvider,
    build_identity_provider,
)


# ---------------------------------------------------------------------------
# Helpers — generate a minimal RSA key pair and test JWTs in-process.
# ---------------------------------------------------------------------------

def _make_rsa_pair():
    """Return (private_key, public_key) for RS256 test tokens."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    return private_key, private_key.public_key()


def _public_key_to_jwk(public_key, kid: str = "test-key-1") -> dict:
    """Convert an RSA public key to a JWK dict."""
    import base64

    pub_numbers = public_key.public_numbers()

    def _b64url(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64url(pub_numbers.n),
        "e": _b64url(pub_numbers.e),
    }


def _sign_token(private_key, claims: dict, kid: str | None = "test-key-1") -> str:
    """Sign *claims* with *private_key* and return a compact JWT string."""
    import jwt

    headers: dict = {"alg": "RS256"}
    if kid:
        headers["kid"] = kid
    return jwt.encode(claims, private_key, algorithm="RS256", headers=headers)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestStaticIdentityProvider(unittest.TestCase):
    def test_returns_static_actor(self):
        provider = StaticIdentityProvider()
        identity = provider.validate_token("any-token")
        self.assertEqual(identity["actor_id"], "static-user")
        self.assertIn("member", identity["roles"])
        self.assertEqual(identity["auth_provider"], "static")


class TestOidcIdentityProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key, cls.public_key = _make_rsa_pair()
        cls.kid = "test-key-1"
        cls.jwk = _public_key_to_jwk(cls.public_key, kid=cls.kid)
        cls.jwks = {"keys": [cls.jwk]}

    def _provider(self, **kwargs) -> OidcIdentityProvider:
        defaults = dict(
            provider="oidc",
            issuer="https://issuer.example",
            audience="ts-harness",
            jwks_url="https://issuer.example/.well-known/jwks.json",
        )
        defaults.update(kwargs)
        config = IdentityConfig(**defaults)
        provider = OidcIdentityProvider(config)
        # Inject JWKS directly to avoid real HTTP fetch.
        provider._jwks_keys = self.jwks["keys"]
        provider._jwks_fetched_at = time.monotonic()
        return provider

    def _token(self, extra: dict | None = None) -> str:
        now = int(time.time())
        claims = {
            "sub": "user-123",
            "email": "user@example.com",
            "iss": "https://issuer.example",
            "aud": "ts-harness",
            "iat": now,
            "exp": now + 3600,
        }
        if extra:
            claims.update(extra)
        return _sign_token(self.private_key, claims, kid=self.kid)

    # --- Happy-path validations ---

    def test_validates_valid_jwt(self):
        provider = self._provider()
        identity = provider.validate_token(self._token())
        self.assertEqual(identity["actor_id"], "user-123")
        self.assertEqual(identity["email"], "user@example.com")
        self.assertEqual(identity["auth_provider"], "oidc")

    def test_maps_sub_to_actor_id(self):
        provider = self._provider()
        identity = provider.validate_token(self._token({"sub": "svc-account-99"}))
        self.assertEqual(identity["actor_id"], "svc-account-99")

    def test_maps_groups_claim_to_groups(self):
        provider = self._provider()
        identity = provider.validate_token(self._token({"groups": ["eng", "infra"]}))
        self.assertEqual(sorted(identity["groups"]), ["eng", "infra"])

    def test_maps_cognito_groups(self):
        provider = self._provider()
        identity = provider.validate_token(self._token({"cognito:groups": ["admin"]}))
        self.assertIn("admin", identity["groups"])

    def test_maps_roles_claim(self):
        provider = self._provider()
        identity = provider.validate_token(self._token({"roles": ["admin", "viewer"]}))
        self.assertEqual(sorted(identity["roles"]), ["admin", "viewer"])

    def test_maps_namespaced_auth0_roles_claim(self):
        provider = self._provider()
        identity = provider.validate_token(
            self._token({"https://ts-harness/roles": ["admin"]})
        )
        self.assertEqual(identity["roles"], ["admin"])

    def test_defaults_to_member_role_when_no_roles_claim(self):
        provider = self._provider()
        identity = provider.validate_token(self._token())
        self.assertIn("member", identity["roles"])

    # --- Validation failures ---

    def test_rejects_empty_token(self):
        provider = self._provider()
        with self.assertRaises(ValueError):
            provider.validate_token("")

    def test_rejects_expired_token(self):
        import jwt

        provider = self._provider()
        expired_token = self._token({"exp": int(time.time()) - 60})
        with self.assertRaises(jwt.ExpiredSignatureError):
            provider.validate_token(expired_token)

    def test_rejects_wrong_audience(self):
        import jwt

        provider = self._provider()
        wrong_aud_token = self._token({"aud": "other-service"})
        with self.assertRaises(jwt.InvalidAudienceError):
            provider.validate_token(wrong_aud_token)

    def test_rejects_wrong_issuer(self):
        import jwt

        provider = self._provider(issuer="https://expected.example")
        wrong_iss_token = self._token({"iss": "https://rogue.example"})
        with self.assertRaises(jwt.InvalidIssuerError):
            provider.validate_token(wrong_iss_token)

    def test_rejects_token_with_unknown_kid(self):
        provider = self._provider()
        bad_kid_token = _sign_token(self.private_key, {
            "sub": "x",
            "iss": "https://issuer.example",
            "aud": "ts-harness",
            "exp": int(time.time()) + 3600,
        }, kid="unknown-kid")
        with self.assertRaises(ValueError):
            provider.validate_token(bad_kid_token)

    def test_rejects_malformed_jwt(self):
        provider = self._provider()
        with self.assertRaises((ValueError, Exception)):
            provider.validate_token("not.a.jwt")

    # --- JWKS caching ---

    def test_jwks_is_cached_within_ttl(self):
        config = IdentityConfig(
            provider="oidc",
            issuer="https://issuer.example",
            audience="ts-harness",
            jwks_url="https://issuer.example/.well-known/jwks.json",
        )
        provider = OidcIdentityProvider(config)
        fetch_count = 0

        def fake_load():
            nonlocal fetch_count
            fetch_count += 1
            return self.jwks["keys"]

        provider._load_jwks = fake_load  # type: ignore[method-assign]
        provider.validate_token(self._token())
        provider.validate_token(self._token())
        # Each call to validate_token calls _load_jwks once.
        self.assertEqual(fetch_count, 2)

    def test_jwks_fetched_via_http(self):
        config = IdentityConfig(
            provider="oidc",
            issuer="https://issuer.example",
            audience="ts-harness",
            jwks_url="https://issuer.example/.well-known/jwks.json",
        )
        provider = OidcIdentityProvider(config)
        mock_response = MagicMock()
        mock_response.json.return_value = self.jwks
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.get", return_value=mock_response) as mock_get:
            provider.validate_token(self._token())
            mock_get.assert_called_once_with(
                "https://issuer.example/.well-known/jwks.json", timeout=10
            )

    def test_raises_if_jwks_url_not_configured(self):
        config = IdentityConfig(provider="oidc", jwks_url=None)
        provider = OidcIdentityProvider(config)
        with self.assertRaises(ValueError, msg="should raise when jwks_url is absent"):
            provider._load_jwks()


class TestSamlIdentityProvider(unittest.TestCase):
    def test_raises_not_implemented_with_guidance(self):
        provider = SamlIdentityProvider(IdentityConfig(provider="saml"))
        with self.assertRaises(NotImplementedError) as ctx:
            provider.validate_token("some-saml-assertion")
        self.assertIn("oidc", str(ctx.exception).lower())

    def test_saml_error_message_mentions_config_path(self):
        provider = SamlIdentityProvider(IdentityConfig(provider="saml"))
        with self.assertRaises(NotImplementedError) as ctx:
            provider.validate_token("x")
        self.assertIn("identity.provider", str(ctx.exception))


class TestBuildIdentityProvider(unittest.TestCase):
    def test_builds_oidc_provider(self):
        config = IdentityConfig(provider="oidc", jwks_url="https://example.com/jwks")
        provider = build_identity_provider(config)
        self.assertIsInstance(provider, OidcIdentityProvider)

    def test_builds_saml_provider(self):
        config = IdentityConfig(provider="saml")
        provider = build_identity_provider(config)
        self.assertIsInstance(provider, SamlIdentityProvider)

    def test_builds_static_provider_for_unknown(self):
        config = IdentityConfig(provider="static")
        provider = build_identity_provider(config)
        self.assertIsInstance(provider, StaticIdentityProvider)

    def test_identity_config_importable_from_identity_module(self):
        """IdentityConfig must remain importable from gateway.identity for back-compat."""
        from ts_harness.gateway.identity import IdentityConfig as IC  # noqa: F401
        self.assertIsNotNone(IC)

    def test_identity_config_importable_from_schema(self):
        from ts_harness.config.schema import IdentityConfig as IC  # noqa: F401
        self.assertIsNotNone(IC)


class TestAuthWiresOidcProvider(unittest.TestCase):
    """authenticate_request should delegate to OidcIdentityProvider when provided."""

    @classmethod
    def setUpClass(cls):
        cls.private_key, cls.public_key = _make_rsa_pair()
        cls.kid = "test-key-1"
        cls.jwk = _public_key_to_jwk(cls.public_key, kid=cls.kid)
        cls.jwks = {"keys": [cls.jwk]}

    def _provider(self):
        config = IdentityConfig(
            provider="oidc",
            issuer="https://issuer.example",
            audience="ts-harness",
            jwks_url="https://issuer.example/.well-known/jwks.json",
        )
        provider = OidcIdentityProvider(config)
        provider._jwks_keys = self.jwks["keys"]
        provider._jwks_fetched_at = time.monotonic()
        return provider

    def _token(self) -> str:
        now = int(time.time())
        return _sign_token(self.private_key, {
            "sub": "alice",
            "email": "alice@example.com",
            "iss": "https://issuer.example",
            "aud": "ts-harness",
            "iat": now,
            "exp": now + 3600,
            "roles": ["admin"],
        }, kid=self.kid)

    def test_auth_context_built_from_jwt_claims(self):
        from ts_harness.gateway.auth import authenticate_request

        ctx = authenticate_request(
            {"Authorization": f"Bearer {self._token()}"},
            expected_token="ignored",
            identity_provider=self._provider(),
        )
        self.assertEqual(ctx.actor_id, "alice")
        self.assertEqual(ctx.email, "alice@example.com")
        self.assertIn("admin", ctx.roles)
        self.assertEqual(ctx.auth_provider, "oidc")

    def test_auth_raises_for_invalid_jwt_when_oidc(self):
        from ts_harness.gateway.auth import authenticate_request, AuthError

        with self.assertRaises(AuthError):
            authenticate_request(
                {"Authorization": "Bearer not-a-jwt"},
                expected_token="ignored",
                identity_provider=self._provider(),
            )

    def test_auth_raises_for_missing_token_when_oidc(self):
        from ts_harness.gateway.auth import authenticate_request, AuthError

        with self.assertRaises(AuthError):
            authenticate_request(
                {},
                expected_token="ignored",
                identity_provider=self._provider(),
            )

    def test_service_token_accepted_when_oidc(self):
        from ts_harness.gateway.auth import authenticate_request

        ctx = authenticate_request(
            {"Authorization": "Bearer svc-secret-123"},
            expected_token="svc-secret-123",
            body={"tenant_id": "acme", "actor": {"actor_id": "workflow-runner"}},
            identity_provider=self._provider(),
        )
        self.assertEqual(ctx.actor_id, "workflow-runner")
        self.assertEqual(ctx.tenant_id, "acme")
        self.assertEqual(ctx.auth_provider, "service")

    def test_service_token_cannot_claim_another_auth_provider(self):
        from ts_harness.gateway.auth import authenticate_request

        ctx = authenticate_request(
            {"Authorization": "Bearer svc-secret-123"},
            expected_token="svc-secret-123",
            body={"actor": {"actor_id": "alice", "auth_provider": "oidc"}},
            identity_provider=self._provider(),
        )
        self.assertEqual(ctx.auth_provider, "service")

    def test_wrong_static_token_rejected_when_oidc(self):
        from ts_harness.gateway.auth import AuthError, authenticate_request

        with self.assertRaises(AuthError):
            authenticate_request(
                {"Authorization": "Bearer svc-secret-12"},
                expected_token="svc-secret-123",
                identity_provider=self._provider(),
            )

    def test_default_or_empty_service_token_rejected_when_oidc(self):
        from ts_harness.gateway.auth import AuthError, authenticate_request

        for expected in ("dev-token", ""):
            with self.subTest(expected=expected), self.assertRaises(AuthError):
                authenticate_request(
                    {"Authorization": f"Bearer {expected}"},
                    expected_token=expected,
                    identity_provider=self._provider(),
                )


class OidcTenantBindingTests(unittest.TestCase):
    def _provider(self, tenant: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            validate_token=lambda token: {
                "actor_id": "alice",
                "email": "alice@example.com",
                "groups": [],
                "roles": ["member"],
                "auth_provider": "oidc",
                "tenant_id": tenant,
            }
        )

    def test_body_tenant_must_match_the_claim(self):
        from ts_harness.gateway.auth import AuthError, authenticate_request

        with self.assertRaises(AuthError) as caught:
            authenticate_request(
                {"Authorization": "Bearer jwt"},
                expected_token="svc",
                body={"tenant_id": "other"},
                single_tenant_mode=False,
                identity_provider=self._provider("acme"),
            )
        self.assertEqual(caught.exception.status, 403)

    def test_missing_body_uses_the_claim_and_auth_writes_it_back(self):
        from types import SimpleNamespace
        from unittest import mock

        from ts_harness.gateway.asgi_app import _auth
        from ts_harness.gateway.auth import authenticate_request

        ctx = authenticate_request(
            {"Authorization": "Bearer jwt"},
            expected_token="svc",
            body={},
            single_tenant_mode=False,
            identity_provider=self._provider("acme"),
        )
        self.assertEqual(ctx.tenant_id, "acme")

        runtime = SimpleNamespace(
            config=SimpleNamespace(
                identity=SimpleNamespace(provider="static"),
                gateway=SimpleNamespace(token="svc", single_tenant_mode=False, default_tenant_id="default"),
            )
        )
        body: dict = {}
        request = SimpleNamespace(headers={"authorization": "Bearer jwt"})
        with mock.patch("ts_harness.gateway.asgi_app.get_runtime", return_value=runtime), mock.patch(
            "ts_harness.gateway.asgi_app.authenticate_request", return_value=ctx
        ):
            _auth(request, body)
        self.assertEqual(body["tenant_id"], "acme")

    def test_missing_claim_and_body_is_rejected(self):
        from ts_harness.gateway.auth import AuthError, authenticate_request

        with self.assertRaises(AuthError) as caught:
            authenticate_request(
                {"Authorization": "Bearer jwt"},
                expected_token="svc",
                body={},
                single_tenant_mode=False,
                identity_provider=self._provider(""),
            )
        self.assertEqual(caught.exception.status, 400)

    def test_single_tenant_mode_ignores_claim_and_body(self):
        from ts_harness.gateway.auth import authenticate_request

        ctx = authenticate_request(
            {"Authorization": "Bearer jwt"},
            expected_token="svc",
            body={"tenant_id": "other"},
            single_tenant_mode=True,
            default_tenant_id="default",
            identity_provider=self._provider("acme"),
        )
        self.assertEqual(ctx.tenant_id, "default")


if __name__ == "__main__":
    unittest.main()
