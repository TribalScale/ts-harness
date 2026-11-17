"""Barrier that freezes an ordered ticket list before the person confirms it."""

from __future__ import annotations

import re
from typing import Any, Callable

from ts_harness.jira.identity import UNMAPPED, resolve_jira_query_user
from ts_harness.jira.ticket_query import DEFAULT_TICKET_STATUSES, build_assigned_ticket_jql
from ts_harness.jira.ticket_queue import order_ticket_queue, wants_ticket_queue

_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,10}$")
_PROJECT_TOKEN = re.compile(r"\b([A-Z][A-Z0-9_]{1,10})\b")
_REPLY_NOISE = frozenset({"NO", "OK", "YES", "READY", "TICKET", "TICKETS", "PROJECT"})
Search = Callable[..., list[dict[str, Any]]]


def format_queue_summary(ordered: list[str], excluded: list[dict[str, str]]) -> str:
    lines = ["Ticket queue:"]
    for index, key in enumerate(ordered, start=1):
        lines.append(f"{index}. {key}")
    lines.append("")
    lines.append("Excluded:")
    if excluded:
        for item in excluded:
            lines.append(f"- {item.get('key')}: {item.get('reason')}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Approve the next step to run these tickets in this order.")
    return "\n".join(lines)


def _summary(task: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    own = task.get("input") if isinstance(task.get("input"), dict) else {}
    if str(own.get("summary") or "").strip():
        return str(own["summary"])
    for item in tasks:
        incoming = item.get("input") if isinstance(item.get("input"), dict) else {}
        text = str(incoming.get("summary") or "").strip()
        if text:
            return text
    return ""


def _explicit_project(source: dict[str, Any]) -> str:
    key = str(source.get("jira_project_key") or "").strip().upper()
    return key if _PROJECT_KEY.fullmatch(key) else ""


def _project_key(task: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    sources: list[dict[str, Any]] = []
    for item in tasks:
        if str(item.get("step_name") or "") != "list_projects":
            continue
        for field in ("output", "input"):
            value = item.get(field)
            if isinstance(value, dict):
                sources.append(value)
    own = task.get("input") if isinstance(task.get("input"), dict) else {}
    sources.append(own)
    for source in sources:
        explicit = _explicit_project(source)
        if explicit:
            return explicit
    for source in sources:
        reply = " ".join(str(source.get(field) or "") for field in ("reply_text", "summary")).upper()
        found = [token for token in _PROJECT_TOKEN.findall(reply) if token not in _REPLY_NOISE]
        if len(found) == 1:
            return found[0]
    return ""


def _account_id(workflow: dict[str, Any], runtime: Any) -> str:
    if runtime is None:
        return UNMAPPED
    from ts_harness.gateway.channels.slack_users import slack_user_id_from_actor

    user_id = slack_user_id_from_actor(str(workflow.get("requester_actor_id") or ""))
    if not user_id:
        return UNMAPPED
    identity = getattr(runtime, "identity", None)
    emails = getattr(identity, "slack_user_emails", None) or {}
    accounts = getattr(identity, "jira_account_ids", None) or {}
    return resolve_jira_query_user(emails.get(user_id), accounts)


def _statuses(memory: Any, workflow: dict[str, Any]) -> list[str]:
    program_id = str(workflow.get("program_id") or "").strip()
    if program_id and hasattr(memory, "get_program"):
        try:
            payload = memory.get_program(program_id) or {}
        except Exception:
            payload = {}
        program = payload.get("program") if isinstance(payload.get("program"), dict) else {}
        snapshot = program.get("program_definition_snapshot")
        config = snapshot.get("config") if isinstance(snapshot, dict) else {}
        picker = config.get("ticket_picker") if isinstance(config, dict) else {}
        statuses = picker.get("statuses") if isinstance(picker, dict) else None
        if isinstance(statuses, list):
            cleaned = [str(item).strip() for item in statuses if str(item).strip()]
            if cleaned:
                return cleaned
    return list(DEFAULT_TICKET_STATUSES)


def _search_issues(search: Search, jql: str) -> list[dict[str, Any]]:
    return list(
        search(
            jql,
            max_results=100,
            fields=["summary", "status", "issuelinks", "parent"],
        )
        or []
    )


def prepare_ticket_queue(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    search: Search | None = None,
    runtime: Any = None,
) -> dict[str, Any]:
    """Freeze ``jira_issue_keys`` for a queue request. A named key leaves the picker alone."""
    del config
    from ts_harness.workflows.system_steps import StepBlocked

    record = memory.get_workflow(str(task.get("workflow_id") or ""))
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    tasks = list(record.get("tasks") or [])
    summary = _summary(task, tasks)
    if not wants_ticket_queue(summary):
        return {"queue": False}

    project = _project_key(task, tasks)
    if not project:
        raise StepBlocked("No Jira project is selected. Ask for a ticket key.", output={"queue": False})

    if runtime is None:
        from ts_harness.implementation.config import load_active_runtime_config

        runtime = load_active_runtime_config()
    account = _account_id(workflow, runtime)
    if account == UNMAPPED:
        raise StepBlocked(
            "No Jira account is mapped for this person. Ask for a ticket key.",
            output={"queue": False},
        )

    jql = build_assigned_ticket_jql(project, _statuses(memory, workflow), account)
    if search is None:
        from ts_harness.integrations.jira_client import JiraClient

        search = JiraClient.from_env().search
    ordered = order_ticket_queue(_search_issues(search, jql))
    exclusions = list(ordered["excluded"])
    keys = list(ordered["ordered"])
    if not keys:
        detail = "; ".join(f"{item['key']}: {item['reason']}" for item in exclusions)
        reason = "No tickets are ready for a queue."
        if detail:
            reason = f"{reason} {detail}"
        else:
            reason = f"{reason} Ask for a ticket key."
        raise StepBlocked(reason, output={"queue": True, "queue_exclusions": exclusions, "jira_issue_keys": []})
    return {
        "queue": True,
        "jira_project_key": project,
        "jira_issue_key": keys[0],
        "jira_issue_keys": keys,
        "queue_exclusions": exclusions,
        "summary": format_queue_summary(keys, exclusions),
    }
