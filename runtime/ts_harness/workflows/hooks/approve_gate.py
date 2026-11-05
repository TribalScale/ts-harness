"""Approval gate handling."""

from __future__ import annotations

import re
from typing import Any

from ts_harness.jira.target_project import jira_choice_from_reply
from ts_harness.workflows.decisions import decision_request_fields
from ts_harness.workflows.hooks import hydrate_input
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.repo_pick import is_repo_pick_stage, repo_pick_output

# Fields a later stage reads from this gate's output. The intake task already
# holds them; the approval reply only adds what the person just said.
_CARRIED_INPUT_KEYS = ("repo_url", "base_branch", "work_order_body", "jira_issue_key")
_NAMED_BRANCH = re.compile(r"\bbranch\s+([A-Za-z0-9][A-Za-z0-9._/-]*)", re.IGNORECASE)


def _carry_gate_inputs(
    output: dict[str, Any] | None,
    task_input: dict[str, Any],
    reply: str,
) -> dict[str, Any] | None:
    """Copy delivery fields onto the gate output without clobbering the reply.

    A branch the person names in the reply wins over the branch already on the
    task. Every other carried key is copied only when the output does not have it.
    """
    carried = dict(output or {})
    named = _NAMED_BRANCH.search(reply or "")
    if named and not str(carried.get("base_branch") or "").strip():
        carried["base_branch"] = named.group(1)
    for key in _CARRIED_INPUT_KEYS:
        if str(carried.get(key) or "").strip():
            continue
        value = task_input.get(key)
        if value in (None, ""):
            continue
        carried[key] = value
    if not carried:
        return output
    return carried


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for approve_gate")
    if not ctx.stage.approval_required:
        raise ValueError("stage does not require approval")
    output = None
    reply = str(ctx.extras.get("reply_text") or "").strip()
    task_input = ctx.task.get("input") if isinstance(ctx.task.get("input"), dict) else {}
    if is_repo_pick_stage(ctx.stage):
        output = repo_pick_output(
            program_id=str(ctx.workflow.get("program_id") or task_input.get("program_id") or ""),
            reply_text=reply,
            request_text=str(task_input.get("summary") or ""),
        )
    elif str(getattr(ctx.stage, "present_prompt", "") or "").strip().lower() == "intake":
        # Later stages read this gate via $prior.<step>.output. The default
        # "Approved <step>" text drops the answers the user just gave.
        if reply:
            answers = [str(item) for item in task_input.get("answers") or [] if str(item).strip()]
            combined = "\n\n".join([*answers, reply])
            output = {"reply_text": reply, "summary": combined}
            if answers:
                output["answers"] = [*answers, reply]
            output.update(jira_choice_from_reply(reply, task_input))
        output = _carry_gate_inputs(output, task_input, reply)
    on_complete = dict(ctx.stage.on_complete or {})
    if on_complete.get("hook") == "freeze_ticket_queue":
        from ts_harness.workflows.hooks import freeze_ticket_queue

        frozen = freeze_ticket_queue.handle(ctx)
        frozen_output = frozen.get("output") if isinstance(frozen, dict) else None
        if isinstance(frozen_output, dict) and frozen_output.get("jira_issue_keys"):
            output = {**(output or {}), **frozen_output}
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

    if isinstance(output, dict):
        # hydrate_downstream reads $prior from this result. approve_task has
        # written it, and the workflow payload may still show the old output.
        ctx.dispatch_result = {"output": output}

    if on_complete.get("hook") == "hydrate_input":
        downstream = list(on_complete.get("downstream") or [])
        if downstream:
            hydrate_input.hydrate_downstream(ctx, downstream)
    elif on_complete.get("hook") in {"mark_artifact_approved", "mark_prd_approved"}:
        from ts_harness.workflows.hooks import mark_artifact_approved

        mark_artifact_approved.handle(ctx)

    return result
