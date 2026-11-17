"""Who may read and write knowledge notes.

Org notes and exceptions are admin-only. A project note can be written by an
admin or a manager of the project's owning team. Reading is everyone who can
already see the project or the org notes page; the admin routes require the
view permission before these helpers run.
"""

from __future__ import annotations

from typing import Any

from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.projects_bff import _is_admin, can_manage


def is_notes_admin(auth: AuthContext, config: Any) -> bool:
    return _is_admin(auth, config)


def can_write_knowledge_note(
    auth: AuthContext,
    config: Any,
    *,
    scope: str,
    project: dict[str, Any] | None = None,
    exception_to: str | None = None,
) -> bool:
    if scope == "org" or exception_to:
        return is_notes_admin(auth, config)
    if scope != "project" or not project:
        return False
    return can_manage(auth, project, config)


def can_see_proposal(auth: AuthContext, config: Any, note: dict[str, Any], project: dict[str, Any] | None) -> bool:
    """Inbox visibility. Org, promotion and exception proposals are admin-only."""
    if note.get("scope") == "org" or note.get("proposal_action") == "promote" or note.get("exception_to"):
        return is_notes_admin(auth, config)
    return can_write_knowledge_note(auth, config, scope="project", project=project)
