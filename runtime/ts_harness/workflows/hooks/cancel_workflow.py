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
    return result
