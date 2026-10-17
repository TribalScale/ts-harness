"""Resolve Slack user IDs to corporate email addresses."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ts_harness.config.schema import RuntimeConfig

_CACHE: dict[str, tuple[float, str | None]] = {}
_CACHE_TTL_SECONDS = int(os.environ.get("SLACK_USER_EMAIL_CACHE_TTL", "900"))


def _cache_get(user_id: str) -> str | None | object:
    entry = _CACHE.get(user_id)
    if not entry:
        return _MISSING
    expires_at, email = entry
    if time.time() > expires_at:
        _CACHE.pop(user_id, None)
        return _MISSING
    return email


class _Missing:
    pass


_MISSING = _Missing()


def _cache_set(user_id: str, email: str | None) -> None:
    _CACHE[user_id] = (time.time() + _CACHE_TTL_SECONDS, email)


def _fetch_email_from_slack(user_id: str, token: str) -> str | None:
    url = f"https://slack.com/api/users.info?user={urllib.request.quote(user_id)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if not payload.get("ok"):
        return None
    profile = (payload.get("user") or {}).get("profile") or {}
    email = str(profile.get("email") or "").strip()
    return email or None


def slack_user_id_from_actor(actor: str) -> str | None:
    raw = str(actor or "").strip().removeprefix("slack:")
    if raw.startswith(("U", "W")) and len(raw) >= 8:
        return raw
    return None


def channel_requester_actor_id(
    session_metadata: dict | None,
    fallback_actor: str,
) -> str:
    """Prefer the Slack user bound to the channel session over orchestrator ids."""
    meta = session_metadata or {}
    slack_user_id = str(meta.get("slack_user_id") or "").strip()
    if slack_user_id:
        return f"slack:{slack_user_id}"
    for raw in (meta.get("requester_actor_id"), fallback_actor):
        actor = str(raw or "").strip()
        user_id = slack_user_id_from_actor(actor)
        if user_id:
            return actor if actor.startswith("slack:") else f"slack:{user_id}"
    return str(fallback_actor or "").strip()


def resolve_slack_user_email(
    user_id: str,
    *,
    config: RuntimeConfig | None = None,
    bot_token: str | None = None,
) -> str | None:
    normalized = str(user_id or "").strip().removeprefix("slack:")
    if not normalized or normalized == "unknown":
        return None

    cached = _cache_get(normalized)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]

    if config is not None:
        mapped = (config.identity.slack_user_emails or {}).get(normalized)
        if mapped:
            _cache_set(normalized, mapped)
            return mapped

    token = (bot_token or os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    email = _fetch_email_from_slack(normalized, token) if token else None
    _cache_set(normalized, email)
    return email
