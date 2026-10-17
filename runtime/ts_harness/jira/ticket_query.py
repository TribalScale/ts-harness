"""Assigned-ticket JQL and Slack list formatting."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

DEFAULT_TICKET_STATUSES = ("To Do", "Open", "Ready for Dev", "In Progress")
NO_EPIC = "No epic"


def _quote_jql(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    if " " in escaped or "-" in escaped or not escaped.isalnum():
        return f'"{escaped}"'
    return escaped


def build_assigned_ticket_jql(
    project: str,
    statuses: list[str] | tuple[str, ...],
    account_id: str,
) -> str:
    project_key = str(project or "").strip()
    account = str(account_id or "").strip()
    if not project_key or not account:
        raise ValueError("project and account_id are required")
    status_list = [str(item).strip() for item in statuses if str(item).strip()]
    if not status_list:
        raise ValueError("at least one status is required")
    in_list = ", ".join(_quote_jql(item) for item in status_list)
    return (
        f"assignee = {_quote_jql(account)} AND project = {_quote_jql(project_key)} "
        f"AND status IN ({in_list})"
    )


def _issue_project_key(issue: dict[str, Any]) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    return str(project.get("key") or issue.get("project_key") or "").strip()


def _issue_epic_label(issue: dict[str, Any]) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
    epic = fields.get("epic") if isinstance(fields.get("epic"), dict) else {}
    key = str(
        parent.get("key")
        or epic.get("key")
        or fields.get("epic_key")
        or issue.get("epic_key")
        or ""
    ).strip()
    name = str(
        parent.get("fields", {}).get("summary")
        if isinstance(parent.get("fields"), dict)
        else epic.get("name") or fields.get("epic_name") or issue.get("epic_name") or ""
    ).strip()
    if not key:
        return NO_EPIC
    return f"{key} — {name}" if name else key


def group_tickets_by_epic(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        label = _issue_epic_label(issue)
        groups.setdefault(label, []).append(issue)
    return [{"epic": label, "issues": items} for label, items in groups.items()]


def projects_from_assigned_issues(issues: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        key = _issue_project_key(issue)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _issue_status(issue: dict[str, Any]) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    return str(status.get("name") or issue.get("status") or "").strip()


def _issue_summary(issue: dict[str, Any]) -> str:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    return str(fields.get("summary") or issue.get("summary") or "").strip()


def format_ticket_list(
    *,
    project: str,
    issues: list[dict[str, Any]],
    pointer_by_key: dict[str, Any] | None = None,
    blocked_keys: set[str] | None = None,
) -> str:
    pointers = pointer_by_key or {}
    blocked = blocked_keys or set()
    lines = [f"Project: {project}", ""]
    grouped = group_tickets_by_epic(issues)
    if not grouped:
        lines.append("No matching assigned tickets.")
        lines.append("")
        lines.append("Reply with a ticket key, or paste a key anytime to skip this list.")
        return "\n".join(lines)
    for group in grouped:
        lines.append(f"Epic {group['epic']}")
        for issue in group["issues"]:
            key = str(issue.get("key") or "").strip()
            pointer = pointers.get(key) if isinstance(pointers.get(key), dict) else {}
            wo = str((pointer or {}).get("work_order_id") or issue.get("work_order_id") or "").strip()
            extra = f"  {wo}" if wo else ""
            flag = "  (blocked)" if key in blocked else ""
            lines.append(f"  {key}  {_issue_summary(issue)}     {_issue_status(issue)}{extra}{flag}")
        lines.append("")
    lines.append("Reply with a ticket key (TFS-184), or paste a key anytime to skip this list.")
    return "\n".join(lines)
