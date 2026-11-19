"""The project a console chat works on, picked in the chat box (``project_key`` on a message).

The pick is kept on the session (``chat_project``). A proposal shows it ("Project: Jamieson Test
(JMT)"), the program it starts gets it as its project, and a program the chat is already linked
to gets it when it has none. Planning reads the project's repositories, templates, conventions
and testing setting through it, and filing uses its Jira key.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SESSION_KEY = "chat_project"


def held_project(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    value = (metadata or {}).get(SESSION_KEY)
    return value if isinstance(value, dict) and value.get("key") else None


def project_label(metadata: dict[str, Any] | None) -> str:
    project = held_project(metadata)
    if not project:
        return ""
    name, key = project.get("name") or "", project["key"]
    return f"{name} ({key})" if name and name != key else key


def apply_chat_project(session: Any, body: dict[str, Any], *, tenant_id: str, memory: Any, actor: str) -> str:
    """Keep the picked project on the session; return a note when it changed the linked program.

    An empty or missing ``project_key`` changes nothing. An unknown key is ignored.
    """
    from ts_harness.agent.sessions import get_session_store

    key = str((body or {}).get("project_key") or "").strip()
    if not key:
        return ""
    current = held_project(session.metadata)
    if not current or current.get("key") != key:
        try:
            rows = (memory.list_projects(tenant_id=tenant_id or "default", key=key) or {}).get("projects") or []
        except Exception as exc:  # noqa: BLE001 - the message still goes through
            logger.info("chat project lookup failed: %s", exc)
            return ""
        project = next((p for p in rows if str(p.get("key") or "") == key), None)
        if project is None:
            return ""
        session.metadata = {**(session.metadata or {}), SESSION_KEY: {"key": key, "name": str(project.get("name") or key)}}
        get_session_store().save(session)
    return set_program_project_if_missing(session, memory=memory, tenant_id=tenant_id, actor=actor)


def set_program_project_if_missing(session: Any, *, memory: Any, tenant_id: str, actor: str) -> str:
    """Give the chat's program the picked project when it has none yet."""
    project = held_project(session.metadata)
    program_id = str((session.metadata or {}).get("program_id") or "")
    if not project or not program_id:
        return ""
    try:
        payload = memory.get_program(program_id) or {}
        program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
        if str(program.get("project_id") or "").strip():
            return ""
        memory.patch_program(
            program_id, tenant_id=tenant_id or "default", project_id=project["key"], project_id_source="chat", actor=actor
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("setting the program's project from the chat failed: %s", exc)
        return ""
    return f"Project set to {project_label(session.metadata)}."
