"""Pending chat proposals: see them and cancel them.

When the harness offers to start something ("I can start the jira_to_pr program … reply yes"),
the offer waits on the chat session until someone answers. A stale one is easy to forget, and a
later "yes" meant for something else could start it. This module lists the proposals waiting on
a session (and across the tenant, for the Inbox) and cancels them: ``/cancel`` in the chat, or
Cancel in the Inbox.
"""

from __future__ import annotations

import re
from typing import Any

CANCEL_COMMAND = re.compile(r"^\s*/cancel(?:\s+(?:the\s+)?proposals?)?\s*$", re.IGNORECASE)

# Session metadata key -> what kind of proposal it holds.
PENDING_KINDS = {
    "pending_program": "Start a program",
    "pending_program_advance": "Move a program to its next phase",
    "pending_workflow": "Start a workflow",
    "pending_delegate": "Hand a task to an agent",
    "pending_bridge": "Continue delivery",
    "pending_continue_delivery": "Continue delivery",
}


def is_cancel_command(text: str) -> bool:
    return bool(CANCEL_COMMAND.match(str(text or "")))


def _describe(key: str, pending: dict[str, Any]) -> str:
    summary = str(pending.get("summary") or pending.get("task") or "").strip()
    target = (
        pending.get("program_type") or pending.get("workflow_type") or pending.get("agent_id")
        or pending.get("target_phase") or ""
    )
    parts = [PENDING_KINDS.get(key, key)]
    if target:
        parts.append(f"`{target}`")
    text = " ".join(parts)
    return f"{text}: {summary[:200]}" if summary else text


def pending_proposals(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``[{"key", "kind", "description", "program_id"}]`` for every proposal waiting on a session."""
    data = metadata if isinstance(metadata, dict) else {}
    found = []
    for key in PENDING_KINDS:
        pending = data.get(key)
        if not isinstance(pending, dict) or not pending:
            continue
        found.append({
            "key": key,
            "kind": PENDING_KINDS[key],
            "description": _describe(key, pending),
            "program_id": str(pending.get("program_id") or data.get("program_id") or ""),
            "proposed_at": str(pending.get("proposed_at") or ""),
        })
    return found


def cancel_proposals(session: Any, *, store: Any = None) -> list[dict[str, Any]]:
    """Drop every proposal waiting on ``session``; returns what was cancelled."""
    cancelled = pending_proposals(session.metadata)
    if not cancelled:
        return []
    metadata = dict(session.metadata or {})
    for item in cancelled:
        metadata.pop(item["key"], None)
    session.metadata = metadata
    if store is None:
        from ts_harness.agent.sessions import get_session_store

        store = get_session_store()
    store.save(session)
    return cancelled


def cancel_ack(cancelled: list[dict[str, Any]]) -> str:
    if not cancelled:
        return "Nothing is waiting for a yes or no in this conversation."
    lines = ["Cancelled:" if len(cancelled) > 1 else "Cancelled the pending proposal:"]
    lines += [f"- {item['description']}" for item in cancelled]
    lines.append("A yes here will not start it now.")
    return "\n".join(lines)


def tenant_proposals(tenant_id: str, *, store: Any = None) -> list[dict[str, Any]]:
    """Every chat session of the tenant with a proposal waiting, for the Inbox."""
    if store is None:
        from ts_harness.agent.sessions import get_session_store

        store = get_session_store()
    rows = []
    for session in store.list_for_tenant(tenant_id) or []:
        for item in pending_proposals(getattr(session, "metadata", None)):
            rows.append({
                **item,
                "session_id": session.session_id,
                "source": getattr(session, "source", ""),
                "started_at": getattr(session, "started_at", ""),
            })
    return rows
