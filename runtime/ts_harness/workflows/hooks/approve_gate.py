"""Approval gate handling."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.decisions import decision_request_fields
from ts_harness.workflows.hooks import hydrate_input
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.repo_pick import is_repo_pick_stage, repo_pick_output


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for approve_gate")
    if not ctx.stage.approval_required:
        raise ValueError("stage does not require approval")
    output = None
    reply = str(ctx.extras.get("reply_text") or "").strip()
    if is_repo_pick_stage(ctx.stage):
        task_input = ctx.task.get("input") if isinstance(ctx.task.get("input"), dict) else {}
        output = repo_pick_output(
            program_id=str(ctx.workflow.get("program_id") or task_input.get("program_id") or ""),
            reply_text=reply,
            request_text=str(task_input.get("summary") or ""),
        )
    elif str(getattr(ctx.stage, "present_prompt", "") or "").strip().lower() == "intake" and reply:
        # Later stages read this gate via $prior.<step>.output. The default
        # "Approved <step>" text drops the answers the user just gave.
        output = {"reply_text": reply, "summary": reply}
    # memory-api approve completes approval-required gates (pending -> succeeded)
    # so the ready-queue never dispatches them to the orchestrator agent.
    result = ctx.memory_client.approve_task(
        str(ctx.task["id"]),
        actor=ctx.actor,
        output=output,
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
