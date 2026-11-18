"""Barrier that freezes an ordered ticket list before the person confirms it."""

from __future__ import annotations

import re
from typing import Any, Callable

from ts_harness.jira.identity import UNMAPPED, jira_account_for_email
from ts_harness.jira.ticket_query import DEFAULT_TICKET_STATUSES, build_assigned_ticket_jql, format_ticket_list, projects_from_assigned_issues
from ts_harness.jira.ticket_queue import named_ticket_key, order_ticket_queue, wants_ticket_queue

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


def _requester_email(workflow: dict[str, Any], runtime: Any, memory: Any) -> str:
    """The email of whoever started the program: Slack's user map, else the sign-in email saved
    on the program at start (``requester_email``)."""
    from ts_harness.gateway.channels.slack_users import slack_user_id_from_actor

    user_id = slack_user_id_from_actor(str(workflow.get("requester_actor_id") or ""))
    if user_id and runtime is not None:
        emails = getattr(getattr(runtime, "identity", None), "slack_user_emails", None) or {}
        if emails.get(user_id):
            return str(emails[user_id])
    program_id = str(workflow.get("program_id") or "").strip()
    if program_id and hasattr(memory, "get_program_status"):
        try:
            artifacts = (memory.get_program_status(program_id) or {}).get("artifacts") or {}
        except Exception:  # noqa: BLE001
            artifacts = {}
        if str(artifacts.get("requester_email") or "").strip():
            return str(artifacts["requester_email"]).strip()
    return ""


def _account_id(email: str, runtime: Any, jira: Any = None) -> str:
    accounts = getattr(getattr(runtime, "identity", None), "jira_account_ids", None) or {}
    return jira_account_for_email(email, accounts, jira)


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


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def assigned_tickets_jql(account: str, statuses: list[str], project: str = "") -> str:
    """Tickets assigned to ``account`` in ``statuses``, in one project or all of them."""
    if project:
        return build_assigned_ticket_jql(project, statuses, account)
    in_list = ", ".join(_quote(s) for s in statuses)
    return f"assignee = {_quote(account)} AND status IN ({in_list}) ORDER BY project, rank"


def _key_project(issue: dict[str, Any]) -> str:
    return str(issue.get("key") or "").upper().split("-", 1)[0]


def _projects(issues: list[dict[str, Any]]) -> list[str]:
    """Projects in listing order: the issue's project field, else its key's prefix."""
    found = projects_from_assigned_issues(issues)
    for issue in issues:
        key = _key_project(issue)
        if key and key not in found:
            found.append(key)
    return found


def _queues(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each project, its ready tickets in delivery order (``all <PROJECT>``)."""
    out: dict[str, dict[str, Any]] = {}
    for project in _projects(issues):
        mine = [i for i in issues if _key_project(i) == project]
        ordered = order_ticket_queue(mine)
        out[project] = {"jira_issue_keys": list(ordered["ordered"]), "queue_exclusions": list(ordered["excluded"])}
    return out


def prepare_ticket_queue(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    search: Search | None = None,
    runtime: Any = None,
    jira: Any = None,
) -> dict[str, Any]:
    """List the person's assigned tickets, or freeze a queue for ``implement all``.

    The first step of ``pick_assigned_jira`` (no agent). It finds the person's Jira account
    from their email (Settings mapping, else Jira's user search), lists their tickets in the
    configured statuses grouped by project and epic, and works out each project's queue. The
    ``pick_ticket`` reply then names a ticket key, or ``all <PROJECT>``.
    """
    del config
    from ts_harness.workflows.system_steps import StepBlocked

    record = memory.get_workflow(str(task.get("workflow_id") or ""))
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else {}
    tasks = list(record.get("tasks") or [])
    summary = _summary(task, tasks)
    named = named_ticket_key(summary)
    if named:
        return {"queue": False, "jira_issue_key": named, "summary": f"Ticket {named}."}

    if runtime is None:
        from ts_harness.implementation.config import load_active_runtime_config

        runtime = load_active_runtime_config()
    if jira is None and search is None:
        from ts_harness.integrations.jira_client import JiraClient

        jira = JiraClient.from_env()
    search = search or jira.search
    email = _requester_email(workflow, runtime, memory)
    account = _account_id(email, runtime, jira)
    if account == UNMAPPED:
        who = f"`{email}`" if email else "you"
        raise StepBlocked(
            f"I couldn't find a Jira account for {who}. Paste a ticket key instead (for example `implement THD-33`), "
            "or map the email to a Jira account in System → Settings → Identity.",
            output={"queue": False},
        )

    project = _project_key(task, tasks)
    statuses = _statuses(memory, workflow)
    issues = list(
        search(
            assigned_tickets_jql(account, statuses, project),
            max_results=100,
            fields=["summary", "status", "issuelinks", "parent", "project"],
        )
        or []
    )
    queues = _queues(issues)

    if wants_ticket_queue(summary):
        if not project and len(queues) == 1:
            project = next(iter(queues))
        if not project:
            raise StepBlocked("Which project? Say `implement all <PROJECT>`.", output={"queue": False})
        found = queues.get(project) or {"jira_issue_keys": [], "queue_exclusions": []}
        keys, exclusions = found["jira_issue_keys"], found["queue_exclusions"]
        if not keys:
            detail = "; ".join(f"{item['key']}: {item['reason']}" for item in exclusions)
            raise StepBlocked(
                "No tickets are ready for a queue." + (f" {detail}" if detail else " Ask for a ticket key."),
                output={"queue": True, "queue_exclusions": exclusions, "jira_issue_keys": []},
            )
        return {
            "queue": True,
            "jira_project_key": project,
            "jira_issue_key": keys[0],
            "jira_issue_keys": keys,
            "queue_exclusions": exclusions,
            "summary": format_queue_summary(keys, exclusions),
        }

    if not issues:
        where = f" in {project}" if project else ""
        status_text = ", ".join(statuses)
        text = f"No tickets are assigned to you{where} in {status_text}. Paste a ticket key to implement one anyway."
        return {"queue": False, "tickets": [], "queues": {}, "summary": text}
    sections = [
        format_ticket_list(project=key, issues=[i for i in issues if _key_project(i) == key])
        .rsplit("\n\nReply with", 1)[0]
        for key in queues
    ]
    example = str(issues[0].get("key") or "")
    sections.append(
        f"Reply with a ticket key ({example}) to implement it, or `all <PROJECT>` to implement every ready "
        "ticket in a project, one after another."
    )
    projects = list(queues)
    return {
        "queue": False,
        "jira_project_key": projects[0] if len(projects) == 1 else "",
        "tickets": [str(i.get("key")) for i in issues],
        "queues": queues,
        "summary": "\n\n".join(sections),
    }
