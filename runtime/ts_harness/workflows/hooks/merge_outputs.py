"""Merge outputs from child workflows into join stage."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for merge_outputs")

    config = dict(ctx.stage.join or {})
    output_key = str(config.get("output_key") or "merged_results")
    children = ctx.memory_client.get_workflow_children(ctx.workflow_id)
    merged: list[dict[str, Any]] = []

    for child in children:
        child_id = str(child.get("id") or child.get("workflow_id") or "")
        if not child_id:
            continue
        payload = ctx.memory_client.get_workflow(child_id)
        child_tasks = list(payload.get("tasks") or [])
        terminal = [
            task
            for task in child_tasks
            if task.get("status") in {"succeeded", "failed", "cancelled"}
        ]
        if not all(task.get("status") == "succeeded" for task in terminal if task.get("step_name")):
            return {"status": "waiting", "reason": "children not terminal"}

        outputs = {
            str(task.get("step_name")): task.get("output")
            for task in child_tasks
            if task.get("status") == "succeeded" and task.get("output")
        }
        merged.append({"workflow_id": child_id, "outputs": outputs})

    output = {output_key: merged}
    ctx.memory_client.complete_task_api(
        str(ctx.task["id"]),
        output,
        actor=ctx.actor or "workflow-runner",
    )
    return {"status": "merged", "output": output, "child_count": len(merged)}
