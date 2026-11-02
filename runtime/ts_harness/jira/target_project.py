"""Resolve the Jira project a program phase files into, from tenant config and the request."""

from __future__ import annotations

import re
from typing import Any, Iterable

_ISSUE_KEY = re.compile(r"^([A-Z][A-Z0-9_]+)-\d+$")


def _normalize(keys: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for key in keys or []:
        text = str(key or "").strip().upper()
        if text and text not in out:
            out.append(text)
    return out


def _project_from_issue_key(value: Any) -> str:
    match = _ISSUE_KEY.match(str(value or "").strip().upper())
    return match.group(1) if match else ""


def _mentioned(text: str, allowed: list[str]) -> list[str]:
    found: list[str] = []
    for key in allowed:
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(key)}(?![A-Za-z0-9_])", text, re.IGNORECASE):
            found.append(key)
    return found


def resolve_jira_target(intake: dict[str, Any], allowed_projects: Iterable[Any]) -> str:
    """Pick the target project: explicit key, then an epic or issue key, then a single mention, then a single allowed project."""
    allowed = _normalize(allowed_projects)
    explicit = str(intake.get("jira_project_key") or "").strip().upper()
    if explicit:
        return explicit
    for field in ("jira_epic_key", "jira_issue_key"):
        project = _project_from_issue_key(intake.get(field))
        if project:
            return project
    if not allowed:
        return ""
    texts = [str(intake.get(field) or "") for field in ("summary", "request", "user_request")]
    mentioned = _mentioned(" ".join(texts), allowed)
    if len(mentioned) == 1:
        return mentioned[0]
    if len(allowed) == 1:
        return allowed[0]
    return ""


_ISSUE_KEY_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_-])([A-Z][A-Z0-9_]+-\d+)(?![A-Za-z0-9_])")


def jira_choice_from_reply(reply: str, intake: dict[str, Any]) -> dict[str, str]:
    """Project and epic a human named when confirming a filing gate.

    An issue key in the reply is the epic to file under. Otherwise a single
    allowed project named in the reply wins over the resolved default.
    Returns nothing when the gate input carries no Jira target at all.
    """
    allowed = _normalize(intake.get("jira_allowed_projects") or [])
    if not allowed and not intake.get("jira_project_key") and not intake.get("jira_epic_key"):
        return {}
    known = allowed or _normalize([resolve_jira_target(intake, allowed)])
    text = str(reply or "")
    for match in _ISSUE_KEY_IN_TEXT.finditer(text):
        key = match.group(1)
        project = _project_from_issue_key(key)
        if project in known:
            return {"jira_epic_key": key, "jira_project_key": project}
    mentioned = _mentioned(text, allowed)
    if len(mentioned) == 1:
        return {"jira_project_key": mentioned[0]}
    choice: dict[str, str] = {}
    if intake.get("jira_epic_key"):
        choice["jira_epic_key"] = str(intake["jira_epic_key"]).strip().upper()
    target = resolve_jira_target(intake, allowed)
    if target:
        choice["jira_project_key"] = target
    return choice


def with_jira_target(intake: dict[str, Any], program_defaults: dict[str, Any] | None) -> dict[str, Any]:
    """Add ``jira_project_key`` and ``jira_allowed_projects`` to a phase intake when they can be resolved."""
    from ts_harness.config.bindings import tenant_bindings

    allowed = list(tenant_bindings(program_defaults).jira_allowed_projects)
    target = resolve_jira_target(intake, allowed)
    if not allowed and not target:
        return intake
    out = dict(intake)
    if allowed:
        out["jira_allowed_projects"] = allowed
    if target:
        out["jira_project_key"] = target
    return out
