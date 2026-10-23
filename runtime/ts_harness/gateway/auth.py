"""Bearer token authentication for the TS Harness gateway.

Two authentication modes are supported:

* **static** (default) — compares the ``Authorization: Bearer`` header value
  against the pre-shared ``expected_token`` from ``config.gateway.token``.
  Actor id and email are taken from the request body ``actor`` field.
  Roles and groups are not: a static token always uses ``service_permissions``.

* **oidc** — when an ``IdentityProvider`` is supplied the bearer token is
  treated as a JWT and validated cryptographically.  Actor identity (sub,
  email, groups, roles) is extracted from the JWT claims; the request body
  ``actor`` field is ignored for identity but still provides ``tenant_id``.
  Internal services (workflow runner, release gates, hooks) still present
  the pre-shared ``expected_token``; it authenticates as
  ``auth_provider="service"`` with identity from the request body.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ts_harness.gateway.identity import IdentityProvider


@dataclass
class AuthContext:
    actor_id: str
    tenant_id: str
    email: str | None = None
    display_name: str | None = None
    groups: list[str] | None = None
    roles: list[str] | None = None
    auth_provider: str = "static"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def extract_bearer_token(headers: dict[str, str]) -> str | None:
    auth = headers.get("Authorization") or headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def check_allowed_origin(
    headers: dict[str, str],
    allowed_origins: list[str],
    *,
    require_origin: bool = False,
) -> None:
    """Enforce browser Origin against the allow-list.

    When *require_origin* is True (mutating admin requests) and an allow-list
    is configured, a missing Origin is rejected. Same-origin server tools that
    omit Origin should call with require_origin=False.
    """
    if not allowed_origins:
        return
    origin = headers.get("Origin") or headers.get("origin") or ""
    if not origin:
        if require_origin:
            raise AuthError("origin required", status=403)
        return
    if origin not in allowed_origins:
        raise AuthError("origin not allowed", status=403)


def authenticate_request(
    headers: dict[str, str],
    *,
    expected_token: str,
    body: dict[str, Any] | None = None,
    single_tenant_mode: bool = True,
    default_tenant_id: str = "default",
    identity_provider: "IdentityProvider | None" = None,
) -> AuthContext:
    """Authenticate an incoming request and return an ``AuthContext``.

    When *identity_provider* is given (OIDC mode) the bearer token is a JWT
    and is validated via the provider.  Otherwise the token is compared
    against *expected_token* (static mode).
    """
    token = extract_bearer_token(headers)
    body = body or {}

    if identity_provider is not None:
        # --- OIDC path: JWT validation -----------------------------------
        if not token:
            raise AuthError("missing bearer token")
        if _is_service_token(token, expected_token):
            return _context_from_body(body, single_tenant_mode, default_tenant_id, force_provider="service")
        try:
            identity = identity_provider.validate_token(token)
        except (ValueError, Exception) as exc:
            raise AuthError(f"JWT validation failed: {exc}") from exc

        claim_tenant = str(identity.get("tenant_id") or "")
        body_tenant = str(body.get("tenant_id") or "")
        if single_tenant_mode:
            tenant_id = default_tenant_id
        elif claim_tenant and body_tenant and body_tenant != claim_tenant:
            raise AuthError("tenant_id does not match token", status=403)
        else:
            tenant_id = claim_tenant or body_tenant
            if not tenant_id:
                raise AuthError("tenant_id is required", status=400)

        return AuthContext(
            actor_id=str(identity.get("actor_id") or "oidc-user"),
            tenant_id=str(tenant_id),
            email=identity.get("email"),
            display_name=None,
            groups=list(identity.get("groups") or []),
            roles=list(identity.get("roles") or ["member"]),
            auth_provider=str(identity.get("auth_provider", "oidc")),
        )

    # --- Static path: pre-shared token comparison -----------------------
    if not token or not expected_token or not hmac.compare_digest(token.encode(), expected_token.encode()):
        raise AuthError("invalid or missing bearer token")

    return _context_from_body(body, single_tenant_mode, default_tenant_id)


# The gateway falls back to this token when GATEWAY_TOKEN is unset; it must
# never authenticate a service caller in OIDC mode.
_DEFAULT_GATEWAY_TOKEN = "dev-token"


def _is_service_token(token: str, expected_token: str) -> bool:
    if not expected_token or expected_token == _DEFAULT_GATEWAY_TOKEN:
        return False
    return hmac.compare_digest(token.encode(), expected_token.encode())


def _context_from_body(
    body: dict[str, Any],
    single_tenant_mode: bool,
    default_tenant_id: str,
    *,
    force_provider: str | None = None,
) -> AuthContext:
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        if single_tenant_mode:
            tenant_id = default_tenant_id
        else:
            raise AuthError("tenant_id is required", status=400)

    actor = body.get("actor") or {}
    if isinstance(actor, str):
        actor_id = actor
        email = None
        display_name = None
    else:
        actor_id = actor.get("actor_id") or actor.get("id") or "system"
        email = actor.get("email")
        display_name = actor.get("display_name")
    # Roles, groups, and auth_provider come from the credential, never the body.
    # A bearer that could set "roles": ["admin"] or "auth_provider": "oidc" would
    # bypass a narrowed service_permissions bundle.
    groups: list[str] = []
    roles: list[str] = []
    auth_provider = force_provider or "static"

    return AuthContext(
        actor_id=actor_id,
        tenant_id=str(tenant_id),
        email=email,
        display_name=display_name,
        groups=groups,
        roles=roles,
        auth_provider=auth_provider,
    )
