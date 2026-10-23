"""Enterprise identity provider interfaces (OIDC JWT validation / SAML stub)."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx

from ts_harness.config.schema import IdentityConfig

# Re-export so existing imports of IdentityConfig from this module keep working.
__all__ = [
    "IdentityConfig",
    "IdentityProvider",
    "StaticIdentityProvider",
    "OidcIdentityProvider",
    "SamlIdentityProvider",
    "build_identity_provider",
]

_JWKS_CACHE_TTL = 300  # seconds

# Auth0 rejects un-namespaced custom claims like "roles"; the Action should set
# https://ts-harness/roles on the access token instead.
_AUTH0_ROLES_CLAIM = "https://ts-harness/roles"


def _claim_list(claims: dict[str, Any], *keys: str) -> list[str]:
    """Return a string list from the first populated claim in *keys*."""
    values: list[Any] = []
    for key in keys:
        if key in claims and claims[key] not in (None, ""):
            raw = claims[key]
            values = raw if isinstance(raw, list) else [raw]
            break
    if not values:
        for key, raw in claims.items():
            if str(key).endswith("/roles") and raw not in (None, ""):
                values = raw if isinstance(raw, list) else [raw]
                break
    return [str(item) for item in values]


class IdentityProvider(Protocol):
    def validate_token(self, token: str) -> dict[str, Any]: ...


class StaticIdentityProvider:
    """Development/static bearer identity — no cryptographic validation."""

    def validate_token(self, token: str) -> dict[str, Any]:
        return {"actor_id": "static-user", "roles": ["member"], "auth_provider": "static"}


class OidcIdentityProvider:
    """OIDC identity provider with real JWT validation via PyJWT + JWKS.

    Fetches and caches the JSON Web Key Set from ``config.jwks_url``, then
    verifies the token's signature, ``exp``, ``iss`` and ``aud`` claims using
    PyJWT.  The ``sub``, ``email``, ``groups`` and ``roles`` JWT claims are
    mapped to the returned identity dict used by the auth layer.
    """

    def __init__(self, config: IdentityConfig):
        self.config = config
        self._jwks_keys: list[dict[str, Any]] | None = None
        self._jwks_fetched_at: float = 0.0

    # ------------------------------------------------------------------
    # JWKS fetching / caching
    # ------------------------------------------------------------------

    def _load_jwks(self) -> list[dict[str, Any]]:
        """Return cached JWKS keys, refreshing from the JWKS URL when stale."""
        now = time.monotonic()
        if self._jwks_keys is not None and (now - self._jwks_fetched_at) < _JWKS_CACHE_TTL:
            return self._jwks_keys

        url = self.config.jwks_url
        if not url:
            raise ValueError(
                "identity.jwks_url is required for OIDC provider; "
                "set it in the runtime config or switch to identity.provider='static'."
            )

        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self._jwks_keys = list(data.get("keys") or [])
        self._jwks_fetched_at = now
        return self._jwks_keys

    def _get_signing_key(self, kid: str | None, keys: list[dict[str, Any]]) -> Any:
        """Locate and deserialise the matching public key from JWKS."""
        try:
            import jwt as _jwt
        except ImportError as exc:
            raise ImportError(
                "PyJWT is required for OIDC validation: pip install 'PyJWT[crypto]'"
            ) from exc

        for key_data in keys:
            if kid and key_data.get("kid") != kid:
                continue
            kty = key_data.get("kty", "RSA")
            serialised = json.dumps(key_data)
            if kty == "RSA":
                return _jwt.algorithms.RSAAlgorithm.from_jwk(serialised)
            if kty == "EC":
                return _jwt.algorithms.ECAlgorithm.from_jwk(serialised)
            raise ValueError(f"unsupported JWK key type: {kty!r}")

        raise ValueError(
            f"no matching JWKS key found for kid={kid!r}; "
            "check that the token was signed with a key advertised at jwks_url."
        )

    # ------------------------------------------------------------------
    # Token validation
    # ------------------------------------------------------------------

    def validate_token(self, token: str) -> dict[str, Any]:
        """Validate a JWT and return a normalised identity dict.

        Raises ``ValueError`` (or a PyJWT exception) on any failure so the
        caller can convert it to an ``AuthError``.
        """
        if not token:
            raise ValueError("empty bearer token")

        try:
            import jwt as _jwt
        except ImportError as exc:
            raise ImportError(
                "PyJWT is required for OIDC validation: pip install 'PyJWT[crypto]'"
            ) from exc

        try:
            header = _jwt.get_unverified_header(token)
        except _jwt.exceptions.DecodeError as exc:
            raise ValueError(f"malformed JWT header: {exc}") from exc

        kid = header.get("kid")
        alg = header.get("alg", "RS256")

        keys = self._load_jwks()
        signing_key = self._get_signing_key(kid, keys)

        decode_kwargs: dict[str, Any] = {"algorithms": [alg]}
        if self.config.audience:
            decode_kwargs["audience"] = self.config.audience
        if self.config.issuer:
            decode_kwargs["issuer"] = self.config.issuer

        # jwt.decode raises ExpiredSignatureError / InvalidAudienceError etc.
        claims: dict[str, Any] = _jwt.decode(token, signing_key, **decode_kwargs)

        # Map standard OIDC claims → internal identity fields.
        sub: str = claims.get("sub") or "oidc-user"
        email: str | None = claims.get("email")
        # Support both standard 'groups' and AWS Cognito 'cognito:groups'.
        groups: list[str] = _claim_list(claims, "groups", "cognito:groups")
        claim_roles: list[str] = _claim_list(claims, "roles", _AUTH0_ROLES_CLAIM)
        from ts_harness.policy.permissions import resolve_actor_roles

        roles = resolve_actor_roles(
            claim_roles=claim_roles,
            groups=groups,
            email=email,
            external_id=sub,
            config=self.config,
        )

        return {
            "actor_id": sub,
            "email": email,
            "groups": groups,
            "roles": roles,
            "auth_provider": "oidc",
            "issuer": claims.get("iss"),
            "tenant_id": str(claims.get(self.config.tenant_claim or "tenant_id") or ""),
        }


class SamlIdentityProvider:
    """SAML identity provider — improved stub with clear actionable error.

    Full SAML 2.0 assertion validation requires a dedicated SAML library
    (e.g. ``python3-saml``, ``pysaml2``).  Until that is wired in, this class
    raises ``NotImplementedError`` with guidance so operators are not silently
    given a stub identity.

    Recommended migration path: configure your IdP to issue OIDC tokens and
    switch ``identity.provider`` to ``"oidc"`` with a ``jwks_url``.
    """

    def __init__(self, config: IdentityConfig):
        self.config = config

    def validate_token(self, token: str) -> dict[str, Any]:
        raise NotImplementedError(
            "SAML assertion validation is not yet implemented. "
            "To enable enterprise SSO today, ask your IdP to enable OIDC federation "
            "and set identity.provider='oidc' with identity.jwks_url in the runtime config. "
            "Full SAML 2.0 support requires python3-saml (not bundled); "
            "open a ticket to track that integration."
        )


def build_identity_provider(config: IdentityConfig) -> IdentityProvider:
    if config.provider == "oidc":
        return OidcIdentityProvider(config)
    if config.provider == "saml":
        return SamlIdentityProvider(config)
    return StaticIdentityProvider()
