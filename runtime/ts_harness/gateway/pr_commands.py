"""``/revise``, ``/accept`` and ``/stop`` as GitHub PR comments.

A work order whose PR is waiting for a human decision (revision limit, escalated triage) can be
decided from the PR itself, the same as from chat or the Inbox. The commenter must be a known
person: their GitHub login is mapped to an email in System Settings (Identity, GitHub logins),
they hold the ``approve_gate`` permission, and, with OIDC, they may approve the program. The
harness replies on the PR with what it did or why it did not.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

COMMAND = re.compile(r"^\s*/(retry|revise|accept|stop)\b[ \t]*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_command(text: str) -> tuple[str, str] | None:
    match = COMMAND.match(text or "")
    if not match:
        return None
    return match.group(1).lower(), match.group(2).strip()


def _norm(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _stuck_for_pr(memory: Any, engine: Any, pr_url: str, tenant_id: str) -> list[dict[str, Any]]:
    from ts_harness.workflows import human_decisions

    found = []
    for task in memory.list_stuck_tasks(tenant_id=tenant_id) or []:
        summary = human_decisions.stuck_summary(engine, str(task.get("workflow_id") or ""), str(task.get("id") or ""))
        if _norm(summary.get("pr_url") or "") == _norm(pr_url):
            found.append(summary)
    return found


def _commenter(login: str, *, memory: Any, config: Any, tenant_id: str) -> tuple[str, list[str], dict[str, Any]]:
    """(email, roles, directory record) for a GitHub login; email is "" when the login is not mapped."""
    from ts_harness.workflows.decisions import roles_for_email
    from ts_harness.workflows.independent_review import logins_to_emails

    emails = logins_to_emails(getattr(getattr(config, "identity", None), "github_logins", None))
    email = emails.get(str(login or "").strip().lower(), "")
    if not email:
        return "", [], {}
    record: dict[str, Any] = {}
    try:
        payload = memory.directory_user(tenant_id=tenant_id, email=email) or {}
        record = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    except Exception:  # noqa: BLE001 - no directory means config roles only
        record = {}
    roles = [str(role).lower() for role in record.get("roles") or []] + roles_for_email(email, config)
    return email, sorted(set(roles)), record


def _refusal(email: str, roles: list[str], record: dict[str, Any], *, login: str, program_id: str, memory: Any,
             config: Any, tenant_id: str) -> str | None:
    from ts_harness.policy.permissions import effective_bundles, permissions_for_roles
    from ts_harness.workflows.decisions import DecisionActor, refusal_for_permission

    actor = DecisionActor(actor_id=f"github:{login}", email=email, roles=tuple(roles))
    denied = refusal_for_permission(actor, "approve_gate", config)
    if denied:
        return denied
    if getattr(getattr(config, "identity", None), "provider", "static") == "oidc" and program_id:
        from ts_harness.gateway.program_access import ProgramAccessDenied, check_program_access

        context = {
            "subject": str(record.get("subject") or ""),
            "teams": list(record.get("teams") or []),
            "is_admin": "edit_config" in permissions_for_roles(roles, effective_bundles(config)),
        }
        try:
            check_program_access(memory, program_id, context, "approve", tenant_id=tenant_id, route="github_comment")
        except ProgramAccessDenied:
            return "you need approve access to this program"
    return None


def handle_pr_command(
    event: dict[str, Any],
    *,
    tenant_id: str = "default",
    memory: Any = None,
    engine: Any = None,
    config: Any = None,
    reply: Any = None,
) -> dict[str, Any]:
    """Decide the PR's stuck step from a comment. ``status`` is ``no_stuck_step`` when none is waiting."""
    parsed = parse_command(str(event.get("body") or ""))
    if parsed is None:
        return {"status": "not_a_command"}
    action, rest = parsed
    pr_url = str(event.get("pr_url") or "")
    login = str(event.get("author") or "")
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    if engine is None:
        from ts_harness.workflows.engine import get_workflow_engine

        engine = get_workflow_engine()
    if config is None:
        from ts_harness.workflows.decisions import _live_config

        config = _live_config()
    say = reply or _reply_on_pr

    candidates = _stuck_for_pr(memory, engine, pr_url, tenant_id)
    if not candidates:
        return {"status": "no_stuck_step", "action": action}
    words = rest.split(maxsplit=1)
    chosen = candidates[0]
    guidance = rest
    if words:
        named = next((item for item in candidates if item["work_order"].lower() == words[0].lower()), None)
        if named is not None:
            chosen, guidance = named, (words[1] if len(words) > 1 else "")
    work_order = chosen["work_order"]

    email, roles, record = _commenter(login, memory=memory, config=config, tenant_id=tenant_id)
    if not email:
        say(pr_url, f"@{login} I can't act on `/{action}` yet: your GitHub login is not mapped to a person. "
                    "An admin can add it in System Settings, Identity, GitHub logins.")
        return {"status": "unmapped", "action": action, "work_order": work_order}
    denied = _refusal(email, roles, record, login=login, program_id=str(chosen.get("program_id") or ""), memory=memory,
                      config=config, tenant_id=tenant_id)
    if denied:
        say(pr_url, f"@{login} I can't act on `/{action}` for {work_order}: {denied}.")
        return {"status": "denied", "action": action, "work_order": work_order, "error": denied}
    try:
        result = engine.decide_blocked_step(
            chosen["workflow_id"], chosen["task_id"], action, actor=email,
            guidance=guidance if action == "revise" else "", tenant_id=tenant_id,
        )
    except ValueError as exc:
        say(pr_url, f"@{login} `/{action}` for {work_order} did not go through: {exc}")
        return {"status": "error", "action": action, "work_order": work_order, "error": str(exc)}
    done = {
        "retry": f"running {work_order}'s stuck step again",
        "revise": f"sent {work_order} back to OpenCode with your guidance and a fresh set of revisions",
        "accept": f"accepted {work_order} as it is; the delivery continues",
        "stop": f"stopped {work_order}",
    }[action]
    say(pr_url, f"Done, @{login}: {done}.")
    return {"status": "decided", "action": action, "work_order": work_order, "result": result}


def _reply_on_pr(pr_url: str, body: str) -> None:
    from ts_harness.integrations.github_client import github_for, parse_github_pr_url

    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        return
    try:
        github_for(pr_url).comment(parsed[0], parsed[1], parsed[2], body)
    except Exception as exc:  # noqa: BLE001 - the decision stands without the reply
        logger.warning("could not reply on %s: %s", pr_url, exc)
