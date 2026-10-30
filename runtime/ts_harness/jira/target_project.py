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
