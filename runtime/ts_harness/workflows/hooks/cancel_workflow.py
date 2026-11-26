"""Cancel workflow and pending tasks."""

from __future__ import annotations

from typing import Any

from ts_harness.agent.sessions import get_session_store
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext) -> dict[str, Any]:
    reason = str(ctx.extras.get("reason") or "cancelled by user")
    result = ctx.memory_client.cancel_workflow(ctx.workflow_id, actor=ctx.actor, reason=reason)

    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    source_channel = str(workflow.get("source_channel") or "")
    if source_channel:
        for session in get_session_store().list_for_tenant(ctx.tenant_id):
            metadata = session.metadata or {}
            if metadata.get("workflow_id") == ctx.workflow_id:
                metadata = dict(metadata)
                metadata["mode"] = "channel_chat"
                metadata.pop("workflow_id", None)
                metadata.pop("workflow_type", None)
                session.metadata = metadata
                get_session_store().save(session)

    ctx.runner_event = "cancelled"
    ctx.workflow = workflow
    ctx.task = tasks[0] if tasks else None
    program = _end_program_with_its_phase(ctx, workflow, reason)
    if program:
        result = {**(result or {}), "program": program}
    return result


ACTIVE_WORKFLOW_STATUSES = frozenset({"pending", "running", "blocked", "waiting", "queued"})


def _end_program_with_its_phase(ctx: HookContext, workflow: dict[str, Any], reason: str) -> dict[str, Any] | None:
    """Cancelling a program's only running phase ends the program too.

    Without this the program page kept saying "running" with nothing left to run, and its
    in-flight count kept a person's slot. A program with another phase still running, or a ticket
    queue with tickets left, is left alone: cancelling one ticket doesn't cancel the queue.
    """
    program_id = str(workflow.get("program_id") or "")
    if not program_id or workflow.get("parent_workflow_id"):
        return None
    try:
        payload = ctx.memory_client.get_program(program_id)
    except Exception:  # noqa: BLE001 - the workflow is cancelled either way
        return None
    program = payload.get("program") if isinstance(payload.get("program"), dict) else {}
    if str(program.get("status") or "") not in {"running", "blocked", ""}:
        return None
    others = [
        item
        for item in payload.get("workflows") or []
        if isinstance(item, dict)
        and str(item.get("workflow_id") or item.get("id") or "") != str(ctx.workflow_id)
        and str(item.get("status") or "") in ACTIVE_WORKFLOW_STATUSES
    ]
    if others:
        return None
    metadata = program.get("metadata") if isinstance(program.get("metadata"), dict) else {}
    from ts_harness.jira.ticket_queue import remaining_queue_keys

    remaining = [key for key in remaining_queue_keys(metadata.get("artifacts") or {}) if key]
    if len(remaining) > 1:
        return None
    try:
        return ctx.memory_client.cancel_program(
            program_id, actor=ctx.actor or "workflow-runner", reason=f"its running phase was cancelled: {reason}"[:500]
        )
    except Exception:  # noqa: BLE001 - the workflow is cancelled; the program can still be cancelled by hand
        return None
