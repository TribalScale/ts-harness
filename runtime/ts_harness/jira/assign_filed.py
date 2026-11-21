"""Assign the tickets ``file_tickets`` filed (``file_work_orders_as_jira``) to the requester.

The filing agent's Jira tools cannot set an assignee, so this system step does it through the
Jira API. It runs only when the filing input carries ``assignee_account_id`` (the PRD ->
work orders -> Jira route); otherwise it does nothing and posts nothing.
"""

from __future__ import annotations

from typing import Any, Callable


def _filed_keys(tasks: list[dict[str, Any]]) -> tuple[list[str], str]:
    from ts_harness.workflows.program_adapters import _filing_project, _latest_filing, filed_ticket_keys

    filing = _latest_filing(tasks)
    if not filing:
        return [], ""
    epic, keys = filed_ticket_keys(filing["output"], project=_filing_project(tasks, filing))
    return keys, epic


def _allowed_projects(tasks: list[dict[str, Any]]) -> set[str]:
    """``jira_filing.allowed_projects`` as the filing input carries it; empty when not given."""
    for task in tasks:
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        values = task_input.get("jira_allowed_projects")
        if isinstance(values, list) and values:
            return {str(value).strip().upper() for value in values if str(value).strip()}
    return set()


def _assignee(tasks: list[dict[str, Any]]) -> str:
    for task in tasks:
        value = (task.get("input") or {}).get("assignee_account_id") if isinstance(task.get("input"), dict) else None
        if value:
            return str(value)
    return ""


def _previous_keys(tasks: list[dict[str, Any]]) -> list[str]:
    """Tickets a refile replaces (``previous_jira_keys`` on the filing input)."""
    for task in tasks:
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        values = task_input.get("previous_jira_keys")
        if isinstance(values, list) and values:
            return [str(value) for value in values if str(value).strip()]
    return []


def assign_filed_tickets(
    task: dict[str, Any], config: dict[str, Any], *, memory: Any, jira_factory: Callable[[], Any] | None = None
) -> dict[str, Any]:
    del config
    from ts_harness.workflows.hooks._helpers import load_workflow_payload

    _workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    account = _assignee(tasks)
    previous = _previous_keys(tasks)
    if not account and not previous:
        return {}  # nothing asked; an empty output posts nothing
    keys, epic = _filed_keys(tasks)
    allowed = _allowed_projects(tasks)

    def in_scope(key: str) -> bool:
        # The same allowlist that limits filing limits every other Jira write of this workflow.
        return not allowed or key.split("-", 1)[0].upper() in allowed

    keys = [key for key in keys if in_scope(key)]
    previous = [key for key in previous if in_scope(key) and key not in keys and key != epic]
    if not keys:
        return {"assigned": [], "summary": "No filed ticket keys were found to assign."}
    from ts_harness.integrations.jira_client import JiraClient

    jira = (jira_factory or JiraClient.from_env)()
    assigned, failed = [], []
    for key in keys if account else []:
        try:
            jira.update_issue(key, fields={"assignee": {"accountId": account}})
            assigned.append(key)
        except Exception as exc:  # noqa: BLE001 - report it, keep going
            failed.append(f"{key} ({str(exc)[:120]})")
    pointed = []
    note = f"Refiled as {epic or keys[0]}" + (f" with {len(keys)} stories" if keys else "") + ". This ticket is no longer tracked."
    for key in previous:
        try:
            jira.comment(key, note)
            pointed.append(key)
        except Exception:  # noqa: BLE001 - a deleted ticket has nothing to point
            continue
    lines = []
    if assigned:
        lines.append(f"Assigned {', '.join(assigned)} to you.")
    if failed:
        lines.append("Could not assign: " + "; ".join(failed) + ". Assign them in Jira.")
    if pointed:
        lines.append(f"Commented on the replaced tickets {', '.join(pointed)} with the new epic.")
    out: dict[str, Any] = {"assigned": assigned, "failed": failed, "summary": " ".join(lines)}
    if previous:
        out["replaced"] = pointed
    return out
