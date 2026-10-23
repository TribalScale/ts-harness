"""Link channel users (Slack) and browser sign-ins to the people directory."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_LINK_TTL = 300.0
_DISABLED_TTL = 60.0
_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}
_disabled_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_lock = threading.Lock()


def _memory(memory: Any | None) -> Any:
    if memory is not None:
        return memory
    from ts_harness.memory.client import get_memory_client

    return get_memory_client()


def record_sign_in(auth: Any, *, memory: Any | None = None) -> dict[str, Any] | None:
    """Upsert an OIDC user and their team memberships; never fails the sign-in."""
    if getattr(auth, "auth_provider", "") != "oidc":
        return None
    try:
        result = _memory(memory).directory_upsert_user(
            tenant_id=str(auth.tenant_id),
            subject=str(auth.actor_id),
            email=auth.email,
            display_name=getattr(auth, "display_name", None),
            teams=list(getattr(auth, "teams", None) or []),
            roles=list(getattr(auth, "roles", None) or []),
        )
    except Exception as exc:  # noqa: BLE001 - the directory is best-effort at sign-in
        logger.warning("directory upsert failed for %s: %s", auth.actor_id, exc)
        return None
    with _lock:
        for key in [k for k in _cache if k[0] == str(auth.tenant_id)]:
            _cache.pop(key, None)
    return result


def directory_identity_for_email(
    email: str | None, tenant_id: str, *, memory: Any | None = None
) -> dict[str, Any] | None:
    """The active directory user with this verified email, cached for five minutes."""
    address = str(email or "").strip().lower()
    if not address:
        return None
    key = (str(tenant_id), address)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    user: dict[str, Any] | None = None
    try:
        found = _memory(memory).directory_user(tenant_id=str(tenant_id), email=address)
        candidate = found.get("user") if found.get("found") else None
        if candidate and not candidate.get("disabled"):
            user = candidate
    except Exception as exc:  # noqa: BLE001 - unlinked is the safe default
        logger.warning("directory lookup failed for a Slack user: %s", exc)
    with _lock:
        _cache[key] = (now + _LINK_TTL, user)
    return user


def link_channel_session(metadata: dict[str, Any], email: str | None, tenant_id: str, **kwargs: Any) -> dict[str, Any]:
    """Return session metadata with the linked directory subject and teams, or marked unlinked."""
    updated = dict(metadata)
    user = directory_identity_for_email(email, tenant_id, **kwargs)
    if user:
        updated["directory_subject"] = user["subject"]
        updated["directory_teams"] = list(user.get("teams") or [])
        updated["directory_roles"] = list(user.get("roles") or [])
        updated["directory_linked"] = True
    else:
        updated.pop("directory_subject", None)
        updated.pop("directory_teams", None)
        updated.pop("directory_roles", None)
        updated["directory_linked"] = False
    return updated


def subject_disabled(tenant_id: str, subject: str, *, memory: Any | None = None) -> bool:
    """Whether the directory has disabled this person, cached for a minute.

    A failed lookup counts as not disabled, so a memory API outage does not
    lock everyone out; the identity provider still refuses new tokens.
    """
    key = (str(tenant_id), str(subject))
    now = time.monotonic()
    with _lock:
        cached = _disabled_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    disabled = False
    try:
        found = _memory(memory).directory_user(tenant_id=str(tenant_id), subject=str(subject))
        disabled = bool(found.get("found") and (found.get("user") or {}).get("disabled"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("directory disabled check failed: %s", exc)
    with _lock:
        _disabled_cache[key] = (now + _DISABLED_TTL, disabled)
    return disabled


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _disabled_cache.clear()
