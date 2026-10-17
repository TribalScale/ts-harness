"""HttpOnly console session cookie.

The admin UI and the API share an origin, so one cookie covers every console
call. Secure follows the request scheme because the console is often plain HTTP
behind a tunnel.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from ts_harness.gateway.auth import AuthError, authenticate_request

SESSION_COOKIE = "ec_session"
STATIC_MAX_AGE = 8 * 60 * 60


def read_session_cookie(headers: dict[str, str]) -> str | None:
    raw = ""
    for key, value in headers.items():
        if str(key).lower() == "cookie":
            raw = value or ""
            break
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE and value:
            return value
    return None


def cookie_secure(headers: dict[str, str]) -> bool:
    flag = os.environ.get("TS_HARNESS_SESSION_COOKIE_SECURE", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    if flag in {"0", "false", "no"}:
        return False
    proto = ""
    for key, value in headers.items():
        if str(key).lower() == "x-forwarded-proto":
            proto = value or ""
            break
    return proto.split(",")[0].strip().lower() == "https"


def session_cookie_header(token: str, *, secure: bool, max_age: int) -> str:
    parts = [
        f"{SESSION_COOKIE}={token}",
        "HttpOnly",
        "SameSite=Lax",
        "Path=/",
        f"Max-Age={max(0, int(max_age))}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie_header(*, secure: bool) -> str:
    parts = [
        f"{SESSION_COOKIE}=",
        "HttpOnly",
        "SameSite=Lax",
        "Path=/",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def session_max_age(token: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        return STATIC_MAX_AGE
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = int(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return STATIC_MAX_AGE
    return max(0, exp - int(time.time()))


def request_scheme_host(headers: dict[str, str], *, scheme: str, host: str) -> tuple[str, str]:
    proto = ""
    header_host = ""
    for key, value in headers.items():
        lowered = str(key).lower()
        if lowered == "x-forwarded-proto" and value:
            proto = value.split(",")[0].strip()
        elif lowered == "host" and value:
            header_host = value
    return proto or scheme or "http", header_host or host


def enforce_cookie_origin(
    headers: dict[str, str],
    *,
    allowed_origins: list[str] | None,
    scheme: str,
    host: str,
) -> None:
    origin = ""
    for key, value in headers.items():
        if str(key).lower() == "origin":
            origin = value or ""
            break
    own_scheme, own_host = request_scheme_host(headers, scheme=scheme, host=host)
    own = f"{own_scheme}://{own_host}"
    allowed = set(allowed_origins or [])
    if origin and (origin == own or origin in allowed):
        return
    raise AuthError("origin not allowed", status=403)


def _static_fallback_allowed() -> bool:
    return os.environ.get("TS_HARNESS_OIDC_STATIC_FALLBACK", "false").lower() in {
        "1",
        "true",
        "yes",
    }


def open_browser_session(
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    runtime: Any,
) -> tuple[int, dict[str, Any], str | None]:
    """Validate a pasted or exchanged token and return the Set-Cookie value."""
    token = str(body.get("access_token") or "").strip()
    if not token:
        return 400, {"error": "access_token is required"}, None
    auth_headers = {str(key).lower(): value for key, value in headers.items()}
    auth_headers["authorization"] = f"Bearer {token}"
    identity = runtime.config.identity
    identity_provider = None
    if getattr(identity, "provider", "static") == "oidc":
        from ts_harness.gateway.identity import build_identity_provider

        identity_provider = build_identity_provider(identity)
    try:
        auth = authenticate_request(
            auth_headers,
            expected_token=runtime.config.gateway.token,
            body={},
            single_tenant_mode=runtime.config.gateway.single_tenant_mode,
            default_tenant_id=runtime.config.gateway.default_tenant_id,
            identity_provider=identity_provider,
        )
    except AuthError as exc:
        return exc.status, {"error": str(exc)}, None
    if getattr(identity, "provider", "static") == "oidc" and auth.auth_provider == "static":
        if not _static_fallback_allowed():
            return 403, {"error": "static token is not accepted"}, None
    from ts_harness.policy.permissions import actor_permissions

    payload = {
        "authenticated": True,
        "roles": [str(role) for role in (auth.roles or [])],
        "permissions": sorted(actor_permissions(auth, runtime.config)),
    }
    cookie = session_cookie_header(
        token,
        secure=cookie_secure(headers),
        max_age=session_max_age(token),
    )
    return 200, payload, cookie


def close_browser_session(headers: dict[str, str]) -> tuple[int, dict[str, Any], str]:
    return 200, {"authenticated": False}, clear_session_cookie_header(secure=cookie_secure(headers))
