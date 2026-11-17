"""Admin handlers for org and project knowledge notes."""

from __future__ import annotations

from typing import Any

from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.knowledge_access import can_write_knowledge_note, is_notes_admin
from ts_harness.gateway.projects_bff import ProjectAccessDenied, _actor, _project, _tenant

_FIELDS = ("kind", "title", "body", "why", "applies_to", "evidence", "exception_to")


def _fields(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in _FIELDS if key in body}


def _require_write(
    auth: AuthContext,
    config: Any,
    *,
    scope: str,
    project: dict[str, Any] | None,
    exception_to: str | None,
) -> None:
    if not can_write_knowledge_note(auth, config, scope=scope, project=project, exception_to=exception_to):
        if scope == "org" or exception_to:
            raise ProjectAccessDenied("only admins can change org notes and exceptions")
        raise ProjectAccessDenied("only admins and managers of the owning team can change project notes")


def _note_project(memory: Any, auth: AuthContext, note: dict[str, Any]) -> dict[str, Any] | None:
    if note.get("scope") != "project" or not note.get("project_id"):
        return None
    return _project(memory, auth, str(note["project_id"]))


def _guard_existing(memory: Any, auth: AuthContext, config: Any, note_id: str) -> dict[str, Any]:
    note = memory.get_knowledge_note(note_id)
    if note.get("tenant_id") not in {None, "", _tenant(auth)} and note.get("tenant_id") != _tenant(auth):
        raise LookupError("note not found")
    project = _note_project(memory, auth, note)
    _require_write(
        auth,
        config,
        scope=str(note.get("scope") or ""),
        project=project,
        exception_to=note.get("exception_to"),
    )
    return note


def list_org_notes_admin(memory: Any, auth: AuthContext, config: Any, _body: dict[str, Any]) -> dict[str, Any]:
    listed = memory.list_knowledge_notes(tenant_id=_tenant(auth), scope="org")
    return {
        "notes": listed.get("notes") or [],
        "is_admin": is_notes_admin(auth, config),
        "can_write": is_notes_admin(auth, config),
    }


def create_org_note_admin(
    memory: Any, auth: AuthContext, config: Any, body: dict[str, Any]
) -> dict[str, Any]:
    _require_write(auth, config, scope="org", project=None, exception_to=None)
    status = "proposed" if body.get("status") == "proposed" else "active"
    return memory.create_knowledge_note(
        tenant_id=_tenant(auth),
        scope="org",
        source="manual",
        status=status,
        actor=_actor(auth),
        **_fields(body),
    )


def list_project_notes(
    memory: Any, auth: AuthContext, config: Any, project_id: str
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    listed = memory.list_knowledge_notes(tenant_id=_tenant(auth), scope="project", project_id=project["id"])
    return {
        "notes": listed.get("notes") or [],
        "is_admin": is_notes_admin(auth, config),
        "can_write": can_write_knowledge_note(auth, config, scope="project", project=project),
    }


def create_project_note(
    memory: Any, auth: AuthContext, config: Any, project_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    exception_to = str(body.get("exception_to") or "").strip() or None
    _require_write(auth, config, scope="project", project=project, exception_to=exception_to)
    status = "proposed" if body.get("status") == "proposed" else "active"
    return memory.create_knowledge_note(
        tenant_id=_tenant(auth),
        scope="project",
        project_id=project["id"],
        source="manual",
        status=status,
        actor=_actor(auth),
        **_fields(body),
    )


def promote_project_note(
    memory: Any, auth: AuthContext, config: Any, project_id: str, note_id: str
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_write(auth, config, scope="project", project=project, exception_to=None)
    return memory.promote_knowledge_note(note_id, actor=_actor(auth), source="manual")


def update_note_admin(
    memory: Any, auth: AuthContext, config: Any, note_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    _guard_existing(memory, auth, config, note_id)
    return memory.update_knowledge_note(note_id, actor=_actor(auth), **_fields(body), expected_version=body.get("expected_version"))


def set_note_status_admin(
    memory: Any, auth: AuthContext, config: Any, note_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    _guard_existing(memory, auth, config, note_id)
    return memory.set_knowledge_note_status(
        note_id,
        status=body.get("status"),
        expected_version=body.get("expected_version"),
        reason=body.get("reason") or "",
        actor=_actor(auth),
    )


def note_versions_admin(memory: Any, auth: AuthContext, _config: Any, note_id: str) -> dict[str, Any]:
    note = memory.get_knowledge_note(note_id)
    if note.get("scope") == "project" and note.get("project_id"):
        _project(memory, auth, str(note["project_id"]))
    return memory.knowledge_note_versions(note_id)


def restore_note_admin(
    memory: Any, auth: AuthContext, config: Any, note_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    _guard_existing(memory, auth, config, note_id)
    return memory.restore_knowledge_note(
        note_id,
        version=body.get("version"),
        expected_version=body.get("expected_version"),
        actor=_actor(auth),
    )
