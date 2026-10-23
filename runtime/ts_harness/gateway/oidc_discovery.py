"""OIDC discovery: read endpoints from ``{issuer}/.well-known/openid-configuration``."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SUCCESS_TTL = 3600.0
_FAILURE_TTL = 60.0

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def discover(issuer: str | None, *, client: Any = None) -> dict[str, Any]:
    """Return the discovery document, or ``{}`` when it cannot be fetched.

    Successful lookups are cached for an hour; failures for a minute so a
    provider outage does not turn every request into a network call.
    """
    issuer = str(issuer or "").strip()
    if not issuer or issuer.startswith("{{"):
        return {}
    now = time.monotonic()
    with _lock:
        cached = _cache.get(issuer)
        if cached and cached[0] > now:
            return cached[1]
    doc: dict[str, Any] = {}
    ttl = _FAILURE_TTL
    try:
        getter = client.get if client is not None else httpx.get
        resp = getter(discovery_url(issuer), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            doc = data
            ttl = _SUCCESS_TTL
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        logger.warning("OIDC discovery failed for %s: %s", issuer, exc)
    with _lock:
        _cache[issuer] = (now + ttl, doc)
    return doc


def clear_cache() -> None:
    with _lock:
        _cache.clear()
