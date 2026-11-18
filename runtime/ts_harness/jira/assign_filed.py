"""Assign the tickets ``file_tickets`` filed (``file_work_orders_as_jira``) to the requester.

The filing agent's Jira tools cannot set an assignee, so this system step does it through the
Jira API. It runs only when the filing input carries ``assignee_account_id`` (the PRD ->
work orders -> Jira route); otherwise it does nothing and posts nothing.
"""

from __future__ import annotations

from typing import Any, Callable


def _filed_keys(tasks: list[dict[str, Any]]) -> tuple[list[str], str]:
    from ts_harness.workflows.program_adapters import _filed_issue_keys

    filed = [t for t in tasks if t.get("step_name") == "file_tickets" and t.get("status") == "succeeded"]
    if not filed:
        return [], ""
    latest = max(filed, key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""))
    output = latest.get("output") if isinstance(latest.get("output"), dict) else {}
    keys = [str(k) for k in output.get("jira_story_keys") or output.get("story_keys") or [] if k]
    epic = str(output.get("jira_epic_key") or output.get("epic_key") or "")
    if not keys:
        epic_found, keys = _filed_issue_keys(str(output.get("summary") or ""))
        epic = epic or (epic_found or "")
    return keys, epic


def _assignee(tasks: list[dict[str, Any]]) -> str:
    for task in tasks:
        value = (task.get("input") or {}).get("assignee_account_id") if isinstance(task.get("input"), dict) else None
        if value:
            return str(value)
    return ""


def assign_filed_tickets(
    task: dict[str, Any], config: dict[str, Any], *, memory: Any, jira_factory: Callable[[], Any] | None = None
) -> dict[str, Any]:
    del config
    from ts_harness.workflows.hooks._helpers import load_workflow_payload

    _workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    account = _assignee(tasks)
    if not account:
        return {}  # nothing asked; an empty output posts nothing
    keys, _epic = _filed_keys(tasks)
    if not keys:
        return {"assigned": [], "summary": "No filed ticket keys were found to assign."}
    from ts_harness.integrations.jira_client import JiraClient

    jira = (jira_factory or JiraClient.from_env)()
    assigned, failed = [], []
    for key in keys:
        try:
            jira.update_issue(key, fields={"assignee": {"accountId": account}})
            assigned.append(key)
        except Exception as exc:  # noqa: BLE001 - report it, keep going
            failed.append(f"{key} ({str(exc)[:120]})")
    lines = []
    if assigned:
        lines.append(f"Assigned {', '.join(assigned)} to you.")
    if failed:
        lines.append("Could not assign: " + "; ".join(failed) + ". Assign them in Jira.")
    return {"assigned": assigned, "failed": failed, "summary": " ".join(lines)}
