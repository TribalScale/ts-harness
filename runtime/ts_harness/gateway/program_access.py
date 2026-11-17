"""Program-level access checks for admin routes, the inbox and channel commands.

``TS_HARNESS_PROGRAM_ACCESS``:

- ``enforce``: deny with 404 (cannot read) or 403 (can read, needs more).
- ``audit`` (default): compute and log would-be denials, never block.
- ``off``: tenant check only, as before sharing existed.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from ts_harness.observability.logging import log_event

LEVELS = ("none", "read", "write", "approve", "admin")
_ADMIN_AUDIT_WINDOW = 3600.0

_admin_audit_seen: dict[tuple[str, str], float] = {}
_admin_audit_lock = threading.Lock()


def rank(level: str | None) -> int:
    try:
        return LEVELS.index(str(level or "none"))
    except ValueError:
        return 0


def access_mode() -> str:
    mode = os.environ.get("TS_HARNESS_PROGRAM_ACCESS", "audit").strip().lower()
    return mode if mode in {"enforce", "audit", "off"} else "audit"


class ProgramAccessDenied(LookupError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


@dataclass
class ProgramAccess:
    level: str
    visibility: str | None = None
    owner_id: str | None = None
    via: str = "service"
    enforced: bool = False

    def allows(self, need: str) -> bool:
        return rank(self.level) >= rank(need)


def actor_context(auth: Any, config: Any = None) -> dict[str, Any] | None:
    """The actor sent to the memory API; None means a service caller (no filtering)."""
    if getattr(auth, "is_service", False):
        return None
    from ts_harness.policy.permissions import actor_permissions

    try:
        if config is None:
            from ts_harness.gateway.server import get_runtime

            config = get_runtime().config
    except Exception:
        config = None
    is_admin = "edit_config" in actor_permissions(auth, config)
    return {
        "subject": str(getattr(auth, "actor_id", "") or ""),
        "teams": list(getattr(auth, "teams", None) or []),
        "is_admin": is_admin,
    }


def channel_actor_context(session_metadata: dict[str, Any] | None, config: Any = None) -> dict[str, Any] | None:
    """Actor for a Slack-style reply: the linked directory person, or nobody when unlinked.

    Without OIDC there is no directory, so channel replies keep acting as today.
    """
    if getattr(getattr(config, "identity", None), "provider", "static") != "oidc":
        return None
    meta = session_metadata or {}
    from ts_harness.policy.permissions import effective_bundles, permissions_for_roles

    roles = list(meta.get("directory_roles") or [])
    return {
        "subject": str(meta.get("directory_subject") or ""),
        "teams": list(meta.get("directory_teams") or []),
        "is_admin": "edit_config" in permissions_for_roles(roles, effective_bundles(config)),
    }


def _query_actor(actor: dict[str, Any]) -> dict[str, str]:
    return {
        "actor_subject": actor["subject"],
        "actor_teams": ",".join(f"{t['slug']}:{t.get('role') or 'member'}" for t in actor.get("teams") or []),
        "actor_admin": "1" if actor.get("is_admin") else "0",
    }


def list_actor_params(auth: Any, config: Any = None) -> dict[str, str]:
    """Query parameters that make the program list show only readable programs (enforce mode)."""
    if access_mode() != "enforce":
        return {}
    actor = actor_context(auth, config)
    return {} if actor is None else _query_actor(actor)


def _audit_admin_access(memory: Any, program_id: str, actor_id: str, *, route: str, need: str) -> None:
    key = (actor_id, program_id)
    now = time.monotonic()
    with _admin_audit_lock:
        last = _admin_audit_seen.get(key)
        if last is not None and now - last < _ADMIN_AUDIT_WINDOW:
            return
        _admin_audit_seen[key] = now
    try:
        memory.record_program_admin_access(program_id, actor=actor_id, route=route, need=need)
    except Exception as exc:  # noqa: BLE001 - auditing must not break support access
        log_event("program_admin_access_audit_failed", program_id=program_id, error=str(exc))


def check_program_access(
    memory: Any,
    program_id: str,
    actor: dict[str, Any] | None,
    need: str,
    *,
    tenant_id: str,
    route: str = "",
) -> ProgramAccess:
    """Return the actor's access or raise ``ProgramAccessDenied`` (enforce mode only)."""
    mode = access_mode()
    if mode == "off" or actor is None:
        return ProgramAccess(level="admin")
    try:
        info = memory.program_access_level(program_id, actor)
        if not isinstance(info, dict):
            raise TypeError("memory API returned no access level")
    except Exception as exc:  # noqa: BLE001
        if mode == "enforce":
            raise ProgramAccessDenied("program not found", 404) from exc
        log_event("program_access_check_failed", program_id=program_id, error=str(exc))
        return ProgramAccess(level="admin")
    if not info.get("found") or str(info.get("tenant_id") or "default") != str(tenant_id or "default"):
        raise ProgramAccessDenied("program not found", 404)
    access = ProgramAccess(
        level=str(info.get("level") or "none"),
        visibility=info.get("visibility"),
        owner_id=info.get("owner_id"),
        via=str(info.get("via") or "service"),
        enforced=mode == "enforce",
    )
    if not access.allows(need):
        log_event(
            "program_access_denied" if mode == "enforce" else "program_access_would_deny",
            program_id=program_id,
            actor=actor.get("subject"),
            need=need,
            level=access.level,
            route=route,
        )
        if mode == "enforce":
            if access.allows("read"):
                raise ProgramAccessDenied(f"needs {need} access to this program", 403)
            raise ProgramAccessDenied("program not found", 404)
        return access
    if access.via == "harness_admin" and rank(info.get("base_level")) < rank(need):
        _audit_admin_access(memory, program_id, str(actor.get("subject") or ""), route=route, need=need)
    return access


def reset_admin_audit_cache() -> None:
    with _admin_audit_lock:
        _admin_audit_seen.clear()


UNLINKED_CHANNEL_MESSAGE = (
    "Your chat account is not linked yet. Sign in to the admin console once with the same "
    "email address, then try again."
)
CHANNEL_SOURCES = frozenset({"slack", "discord", "telegram"})


def channel_program_refusal(
    session_metadata: dict[str, Any] | None,
    *,
    tenant_id: str,
    need: str = "approve",
    channel: str = "slack",
    auth: Any = None,
    config: Any = None,
    memory: Any = None,
    program_id: str | None = None,
    task_id: str | None = None,
    route: str = "channel",
) -> str | None:
    """Refusal text when a chat reply may not act on its program, else None."""
    if config is None:
        try:
            from ts_harness.gateway.server import get_runtime

            config = get_runtime().config
        except Exception:
            return None
    if getattr(getattr(config, "identity", None), "provider", "static") != "oidc":
        return None
    meta = session_metadata or {}
    if channel in CHANNEL_SOURCES:
        if meta.get("directory_linked") is False:
            return UNLINKED_CHANNEL_MESSAGE
        actor = channel_actor_context(meta, config)
    elif auth is not None:
        actor = actor_context(auth, config)
    else:
        return None
    if actor is None:
        return None
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    pid = program_id or str(meta.get("program_id") or "").strip() or None
    if not pid and task_id:
        try:
            pid = (memory.task_program(task_id) or {}).get("program_id")
        except Exception:  # noqa: BLE001
            pid = None
    if not pid:
        return None
    try:
        check_program_access(memory, str(pid), actor, need, tenant_id=tenant_id, route=route)
    except ProgramAccessDenied:
        return f"you need {need} access to this program"
    return None
