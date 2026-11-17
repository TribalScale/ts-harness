"""Short-lived proxy tokens: HS256 JWTs the runtime mints and the LLM proxy verifies.

A token carries who the call is for (tenant, team, project, program, workflow,
task, user, agent, coding job) so the proxy can meter and budget it. It never
carries a provider key.
"""

from __future__ import annotations

import os
import time
from typing import Any, Mapping

import jwt

SCOPE_FIELDS = ("tenant", "team", "project", "program", "workflow", "task", "user", "agent", "job")
ISSUER = "ts-harness-runtime"
AUDIENCE = "ts-harness-llm-proxy"
DEFAULT_TTL_SECONDS = 3 * 3600


class InvalidProxyToken(ValueError):
    pass


def proxy_secret(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return str(env.get("LLM_PROXY_SECRET") or "").strip()


def proxy_url(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return str(env.get("LLM_PROXY_URL") or "").strip().rstrip("/")


def proxy_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """The runtime routes model calls through the proxy when it knows the URL and can mint tokens."""
    return bool(proxy_url(environ) and proxy_secret(environ))


def mint_proxy_token(
    scope: Mapping[str, Any],
    *,
    secret: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    key = secret if secret is not None else proxy_secret()
    if not key:
        raise InvalidProxyToken("LLM_PROXY_SECRET is not set")
    issued = int(now if now is not None else time.time())
    claims: dict[str, Any] = {
        field: str(scope[field]) for field in SCOPE_FIELDS if scope.get(field) not in (None, "")
    }
    claims.update({"iss": ISSUER, "aud": AUDIENCE, "iat": issued, "exp": issued + int(ttl_seconds)})
    return jwt.encode(claims, key, algorithm="HS256")


def verify_proxy_token(token: str, *, secret: str) -> dict[str, str]:
    if not token or not secret:
        raise InvalidProxyToken("missing proxy token")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
    except jwt.PyJWTError as exc:
        raise InvalidProxyToken(str(exc)) from exc
    return {field: str(claims[field]) for field in SCOPE_FIELDS if claims.get(field)}
