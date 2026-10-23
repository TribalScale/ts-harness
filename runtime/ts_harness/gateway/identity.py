"""Enterprise identity provider interfaces (OIDC JWT validation / SAML stub)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

import httpx

from ts_harness.config.schema import IdentityConfig
from ts_harness.gateway.identity_presets import identity_setting
from ts_harness.gateway.oidc_discovery import discover

logger = logging.getLogger(__name__)

# Re-export so existing imports of IdentityConfig from this module keep working.
__all__ = [
    "IdentityConfig",
    "IdentityProvider",
    "StaticIdentityProvider",
    "OidcIdentityProvider",
    "SamlIdentityProvider",
    "GroupResolver",
    "register_group_resolver",
    "build_identity_provider",
]

_JWKS_CACHE_TTL = 300  # seconds

# Auth0 rejects un-namespaced custom claims like "roles"; the Action should set
# https://ts-harness/roles on the access token instead.
_AUTH0_ROLES_CLAIM = "https://ts-harness/roles"
_AUTH0_GROUPS_CLAIM = "https://ts-harness/groups"


class GroupResolver(Protocol):
    """Supplies groups at sign-in for providers that do not put them in tokens."""

    def resolve_groups(self, *, subject: str, email: str | None, claims: dict[str, Any]) -> list[str]: ...


_GROUP_RESOLVERS: dict[str, GroupResolver] = {}


def register_group_resolver(source: str, resolver: GroupResolver | None) -> None:
    if resolver is None:
        _GROUP_RESOLVERS.pop(source, None)
    else:
        _GROUP_RESOLVERS[source] = resolver


def _claim_list(
    claims: dict[str, Any], *keys: str, fallback_suffix: str | None = "/roles"
) -> list[str]:
    """Return a string list from the first populated claim in *keys*."""
    values: list[Any] = []
    for key in keys:
        if key in claims and claims[key] not in (None, ""):
            raw = claims[key]
            values = raw if isinstance(raw, list) else [raw]
            break
    if not values and fallback_suffix:
        for key, raw in claims.items():
            if str(key).endswith(fallback_suffix) and raw not in (None, ""):
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

        url = self.config.jwks_url or discover(self.config.issuer).get("jwks_uri")
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

        token_type = identity_setting(self.config, "token_type")
        audience = self.config.client_id if token_type == "id" else self.config.audience
        decode_kwargs: dict[str, Any] = {"algorithms": [alg]}
        if audience:
            decode_kwargs["audience"] = audience
        if self.config.issuer:
            decode_kwargs["issuer"] = self.config.issuer

        # jwt.decode raises ExpiredSignatureError / InvalidAudienceError etc.
        claims: dict[str, Any] = _jwt.decode(token, signing_key, **decode_kwargs)

        sub: str = claims.get("sub") or "oidc-user"
        email: str | None = claims.get("email")
        if claims.get("email_verified") is False:
            raise ValueError("email address is not verified")
        self._check_allowed_domain(email, claims)

        groups = self._groups(sub, email, claims)
        claim_roles: list[str] = _claim_list(
            claims, identity_setting(self.config, "roles_claim"), "roles", _AUTH0_ROLES_CLAIM
        )
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
            "display_name": claims.get("name") or None,
            "groups": groups,
            "roles": roles,
            "auth_provider": "oidc",
            "issuer": claims.get("iss"),
            "tenant_id": str(claims.get(self.config.tenant_claim or "tenant_id") or ""),
        }

    def _check_allowed_domain(self, email: str | None, claims: dict[str, Any]) -> None:
        allowed = {d.lower() for d in (self.config.allowed_domains or [])}
        if not allowed:
            return
        domains = set()
        if email and "@" in email:
            domains.add(email.rsplit("@", 1)[1].lower())
        if claims.get("hd"):
            domains.add(str(claims["hd"]).lower())
        if not domains & allowed:
            raise ValueError("account domain is not allowed to sign in")

    def _groups(self, sub: str, email: str | None, claims: dict[str, Any]) -> list[str]:
        token_groups = _claim_list(
            claims,
            identity_setting(self.config, "groups_claim"),
            "groups",
            "cognito:groups",
            _AUTH0_GROUPS_CLAIM,
            fallback_suffix=None,
        )
        source = identity_setting(self.config, "groups_source")
        if source == "token":
            return token_groups
        resolver = _GROUP_RESOLVERS.get(source)
        if resolver is None:
            logger.warning("groups_source %s has no resolver; using token groups", source)
            return token_groups
        try:
            resolved = resolver.resolve_groups(subject=sub, email=email, claims=claims)
        except Exception as exc:  # noqa: BLE001 - sign-in must not fail on a directory outage
            logger.warning("group lookup via %s failed: %s", source, exc)
            return token_groups
        return sorted(set(token_groups) | {str(g) for g in resolved})


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
