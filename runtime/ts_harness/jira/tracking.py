"""Keep a Jira ticket's status in step with its delivery (``implement_jira_ticket``).

``prepare_ticket_delivery`` moves the ticket to In Progress when its coding starts, and the
``close_ticket`` step moves it to Done once its pull request merges, with a comment linking the
PR. Transition names come from ``program_defaults.jira_tracking`` (``start_transition``,
``done_transition``); Jira workflows differ, and a name the ticket does not offer is reported,
never fatal. ``enabled: false`` turns both moves off.
"""

from __future__ import annotations

from typing import Any, Callable

DEFAULT_START = "In Progress"
DEFAULT_DONE = "Done"


def tracking_settings(defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    if defaults is None:
        try:
            from ts_harness.workflows.delivery_program import _live_program_defaults

            defaults = _live_program_defaults()
        except Exception:  # noqa: BLE001
            defaults = {}
    raw = dict((defaults or {}).get("jira_tracking") or {})
    return {
        "enabled": raw.get("enabled") is not False,
        "start_transition": str(raw.get("start_transition") or DEFAULT_START),
        "done_transition": str(raw.get("done_transition") or DEFAULT_DONE),
    }


def move_ticket(
    key: str,
    which: str,
    *,
    jira: Any = None,
    settings: dict[str, Any] | None = None,
    comment: str = "",
    jira_factory: Callable[[], Any] | None = None,
) -> str:
    """Apply the ``which`` ("start" or "done") transition; return a warning, or "" when done."""
    settings = settings or tracking_settings()
    if not settings["enabled"] or not key:
        return ""
    name = settings["start_transition" if which == "start" else "done_transition"]
    try:
        if jira is None:
            from ts_harness.integrations.jira_client import JiraClient

            jira = (jira_factory or JiraClient.from_env)()
        moved = jira.transition(key, name)
        if comment:
            jira.comment(key, comment)
    except Exception as exc:  # noqa: BLE001
        return f"could not move {key} to {name}: {str(exc)[:160]}"
    return "" if moved else f"{key} has no transition named {name}; move it in Jira"


def close_ticket(task: dict[str, Any], config: dict[str, Any], *, memory: Any, jira: Any = None) -> dict[str, Any]:
    """System step after the merge: move the ticket to Done and link the merged PR."""
    del config
    from ts_harness.workflows.hooks._helpers import load_workflow_payload

    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    key = ""
    pr_url = ""
    for item in sorted(tasks, key=lambda t: str(t.get("created_at") or "")):
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        key = key or str(output.get("jira_issue_key") or (item.get("input") or {}).get("jira_issue_key") or "")
        if item.get("step_name") == "deliver_work_order" and output.get("pr_url"):
            pr_url = str(output["pr_url"])
    key = key or str((workflow.get("input") or {}).get("jira_issue_key") or workflow.get("external_ref") or "")
    if not key:
        return {"summary": ""}
    note = f"Merged: {pr_url}" if pr_url else "Merged."
    warning = move_ticket(key, "done", jira=jira, comment=f"{note} (TS Harness)")
    if warning:
        return {"jira_issue_key": key, "warnings": [warning], "summary": f"{key}: {warning}."}
    return {"jira_issue_key": key, "summary": f"{key} moved to {tracking_settings()['done_transition']}. {note}"}
