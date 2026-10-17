"""Approval gate handling."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.decisions import decision_request_fields
from ts_harness.workflows.hooks import hydrate_input
from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for approve_gate")
    if not ctx.stage.approval_required:
        raise ValueError("stage does not require approval")
    # memory-api approve completes approval-required gates (pending -> succeeded)
    # so the ready-queue never dispatches them to the orchestrator agent.
    result = ctx.memory_client.approve_task(
        str(ctx.task["id"]),
        actor=ctx.actor,
        **decision_request_fields(ctx.extras.get("decision"), ctx.workflow),
    )
    if result.get("status") == "succeeded" or result.get("approval_status") == "approved":
        result = {**result, "status": "approved"}

    on_complete = dict(ctx.stage.on_complete or {})
    if on_complete.get("hook") == "hydrate_input":
        downstream = list(on_complete.get("downstream") or [])
        if downstream:
            hydrate_input.hydrate_downstream(ctx, downstream)
    elif on_complete.get("hook") in {"mark_artifact_approved", "mark_prd_approved"}:
        from ts_harness.workflows.hooks import mark_artifact_approved

        mark_artifact_approved.handle(ctx)

    return result
