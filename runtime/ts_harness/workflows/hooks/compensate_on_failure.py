"""Create compensate task on stage failure."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ts_harness.workflows.hooks._helpers import create_tasks_bulk, load_workflow_payload
from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for compensate_on_failure")

    config = dict(ctx.stage.on_failure or {})
    compensate_stage_name = str(config.get("compensate_stage") or "").strip()
    if not compensate_stage_name:
        return {"status": "skipped", "reason": "no compensate_stage"}

    compensate_stage = ctx.skeleton.stage(compensate_stage_name)
    if compensate_stage.kind != "compensate":
        raise ValueError(f"compensate stage must have kind compensate: {compensate_stage_name}")

    failed_task = ctx.task
    failure_reason = str(
        (ctx.extras or {}).get("error")
        or failed_task.get("error")
        or "stage failed"
    )
    failed_at = datetime.now(timezone.utc).isoformat()

    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    # Idempotency: do not spawn duplicate compensate tasks for the same failure.
    existing = [
        task
        for task in tasks
        if task.get("step_name") == compensate_stage_name
        and (task.get("input") or {}).get("_failed_task", {}).get("id") == failed_task.get("id")
    ]
    if existing:
        return {
            "status": "compensating",
            "compensate_task_ids": [str(existing[0].get("id"))],
            "compensate_stage": compensate_stage_name,
            "idempotent": True,
        }

    program_id = str((workflow or {}).get("program_id") or "").strip()
    if program_id and hasattr(ctx.memory_client, "patch_program_artifacts"):
        ctx.memory_client.patch_program_artifacts(
            program_id,
            {
                "failed_stage": str(failed_task.get("step_name") or ctx.stage.name),
                "failure_reason": failure_reason,
                "failed_at": failed_at,
                "compensation_status": "triggered",
            },
            actor=ctx.actor or "workflow-runner",
        )

    # Best-effort: clear wait token / lease markers on the failed task if APIs exist.
    if hasattr(ctx.memory_client, "clear_task_wait"):
        try:
            ctx.memory_client.clear_task_wait(str(failed_task["id"]))
        except Exception:
            pass

    task_ids = create_tasks_bulk(
        ctx.memory_client,
        workflow_id=ctx.workflow_id,
        workflow_type=ctx.workflow_type,
        tenant_id=ctx.tenant_id,
        tasks=[
            {
                "step_name": compensate_stage.name,
                "agent_id": compensate_stage.agent_id,
                "depends_on": [str(failed_task["id"])],
                "input": {
                    "_compensate": True,
                    "failed_stage": str(failed_task.get("step_name") or ctx.stage.name),
                    "failure_reason": failure_reason,
                    "failed_at": failed_at,
                    "compensation_status": "triggered",
                    "_failed_task": {
                        "id": failed_task.get("id"),
                        "step_name": failed_task.get("step_name"),
                        "error": failure_reason,
                        "output": failed_task.get("output"),
                    },
                },
                "tenant_id": ctx.tenant_id,
            },
        ],
        actor=ctx.actor or "workflow-runner",
    )

    if workflow and hasattr(ctx.memory_client, "update_workflow_compensation"):
        ctx.memory_client.update_workflow_compensation(ctx.workflow_id, "triggered")

    return {
        "status": "compensating",
        "compensate_task_ids": task_ids,
        "compensate_stage": compensate_stage_name,
        "failed_stage": str(failed_task.get("step_name") or ctx.stage.name),
        "failure_reason": failure_reason,
    }
