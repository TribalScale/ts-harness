"""Console and chat side of pause and resume (``ts_harness.workflows.pause``).

``apply`` is the one path both use: it changes the pause document, then on a resume releases
the held tasks, moves the wait deadlines on and wakes the ticket pools. Permission checks stay
with the caller (``asgi_app`` for the console, ``channel_confirmations`` for chat): ticket and
program pauses need ``approve_gate`` and approve access to the program, the global pause needs
an admin (``edit_config``).
"""

from __future__ import annotations

import logging
from typing import Any

from ts_harness.workflows import pause

logger = logging.getLogger(__name__)

ACTIONS = ("pause", "resume")


class TargetNotFound(LookupError):
    pass


def _workflow_label(payload: dict[str, Any]) -> str:
    from ts_harness.workflows.human_decisions import work_order_label

    tasks = [t for t in payload.get("tasks") or [] if isinstance(t, dict)]
    for task in tasks:
        key = (task.get("input") or {}).get("jira_issue_key") if isinstance(task.get("input"), dict) else None
        if key:
            return str(key)
    label = work_order_label(tasks, payload.get("workflow") or {})
    return "" if label == "this work order" else label


def ticket_target(memory: Any, workflow_id: str, *, tenant_id: str) -> dict[str, Any]:
    """The workflow to pause: its label, whether it is a ticket queue, its program and first task."""
    try:
        payload = memory.get_workflow(workflow_id) or {}
    except Exception as exc:  # noqa: BLE001
        raise TargetNotFound("workflow not found") from exc
    workflow = payload.get("workflow") or {}
    if not workflow or str(workflow.get("tenant_id") or "default") != tenant_id:
        raise TargetNotFound("workflow not found")
    tasks = [t for t in payload.get("tasks") or [] if isinstance(t, dict)]
    task_id = str((tasks[0] if tasks else {}).get("id") or "")
    program_id = str(workflow.get("program_id") or "")
    if not program_id and task_id:
        try:
            program_id = str((memory.task_program(task_id) or {}).get("program_id") or "")
        except Exception:  # noqa: BLE001 - the program is shown, not required
            program_id = ""
    kind = "queue" if str(workflow.get("workflow_type") or "") in pause.QUEUE_TYPES else "ticket"
    return {
        "target_id": str(workflow.get("id") or workflow_id),
        "label": _workflow_label(payload) or str(workflow.get("workflow_type") or "workflow"),
        "kind": kind,
        "program_id": program_id,
        "task_id": task_id,
        "status": str(workflow.get("status") or ""),
    }


def program_target(memory: Any, program_id: str, *, tenant_id: str) -> dict[str, Any]:
    try:
        program = (memory.get_program(program_id) or {}).get("program") or {}
    except Exception as exc:  # noqa: BLE001
        raise TargetNotFound("program not found") from exc
    if not program or str(program.get("tenant_id") or "default") != tenant_id:
        raise TargetNotFound("program not found")
    return {
        "target_id": str(program.get("id") or program_id),
        "label": str(program.get("display_name") or program.get("summary") or program_id)[:200],
        "kind": "program",
        "program_id": str(program.get("id") or program_id),
        "status": str(program.get("status") or ""),
        "parent_programs": [str(program.get(k)) for k in ("parent_program_id", "root_program_id")
                            if program.get(k) and str(program.get(k)) != str(program.get("id") or program_id)],
    }


def apply(
    memory: Any,
    action: str,
    scope: str,
    target: dict[str, Any] | None = None,
    *,
    actor: str,
    tenant_id: str = "default",
    reason: str = "",
) -> dict[str, Any]:
    """Pause or resume; on a resume also release what it held. Idempotent both ways."""
    action = str(action or "").strip().lower()
    if action not in ACTIONS:
        raise pause.PauseError(f"unknown action {action!r}: use pause or resume")
    scope = pause.scope_name(scope)
    target = target or {}
    target_id = str(target.get("target_id") or "")
    if action == "pause":
        result = pause.pause(
            scope, target_id, actor=actor, reason=reason, label=str(target.get("label") or ""),
            kind=str(target.get("kind") or "") if scope == "ticket" else "",
            program_id=str(target.get("program_id") or "") if scope == "ticket" else "", client=memory,
        )
    else:
        result = pause.resume(scope, target_id, actor=actor, client=memory)
        result = pause.release_after_resume(memory, result, tenant_id=tenant_id, actor=actor)
        if result.get("status") == "resumed" and scope != "global":
            # Resumed, but a wider pause still covers it: say so rather than look resumed.
            state = pause.read_state(memory)
            if scope == "ticket" and target.get("task_id"):
                workflows, programs = pause.chains_for_task(memory, str(target["task_id"]), workflow_id=target_id)
            else:
                workflows, programs = [], [str(p) for p in target.get("parent_programs") or [] if p]
            wider = pause.entry_for(state, workflow_chain=workflows, program_chain=programs)
            if wider:
                result["still_paused_by"] = wider
    result["label"] = str(target.get("label") or "")
    result["kind"] = str(target.get("kind") or scope)
    return result


def audit_payload(result: dict[str, Any], *, actor: str, source: str) -> dict[str, Any]:
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    return {
        "scope": result.get("scope"),
        "target_id": result.get("target_id"),
        "label": result.get("label"),
        "status": result.get("status"),
        "reason": entry.get("reason") or "",
        "paused_by": entry.get("by"),
        "paused_at": entry.get("at"),
        "paused_seconds": result.get("paused_seconds"),
        "released": len(result.get("released") or []),
        "actor": actor,
        "source": source,
    }


def message(result: dict[str, Any]) -> str:
    """One line confirming what happened, for chat replies and the console toast."""
    scope = str(result.get("scope") or "")
    label = str(result.get("label") or "")
    what = {
        "global": "All coding",
        "program": f"Program {label}".strip(),
        "ticket": f"{'Ticket queue' if result.get('kind') == 'queue' else ''} {label}".strip() or "The ticket",
    }.get(scope, label or "It")
    status = result.get("status")
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    if status == "already_paused":
        return f"{what} was already paused (by {entry.get('by') or 'someone'} at {str(entry.get('at') or '')[:16].replace('T', ' ')} UTC). Nothing changed."
    if status == "not_paused":
        return f"{what} is not paused, so there was nothing to resume."
    if status == "paused":
        if scope == "global":
            return ("Paused all coding. Running coding jobs, code reviews and QA finish; queued coding jobs stay "
                    "queued and start when an admin resumes from the System page.")
        tail = " No new tickets start." if scope == "program" or result.get("kind") == "queue" else ""
        return (f"Paused {what}. Steps already running finish; the next coding run or revision waits, "
                f"with its feedback kept, until you resume.{tail}")
    if status == "resumed":
        released = len(result.get("released") or [])
        text = f"Resumed {what}."
        if released:
            text += f" {released} held step{'s' if released != 1 else ''} picked up where {'they' if released != 1 else 'it'} stopped."
        if scope == "global":
            text += " Queued coding jobs start again as slots free up."
        wider = result.get("still_paused_by")
        if isinstance(wider, dict):
            text += f" It stays held because its {wider.get('scope')} is still paused (by {wider.get('by') or 'someone'})."
        return text
    return f"{what}: {status}"
