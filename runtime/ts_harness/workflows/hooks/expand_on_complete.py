"""Expand stage completion: validate proposed tasks and propose plan."""

from __future__ import annotations

from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.registry import HookContext

ensure_scripts_importable()

import workflow_definitions  # noqa: E402


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for expand_on_complete")
    if ctx.stage.kind != "expand":
        raise ValueError("expand_on_complete requires expand stage")

    proposed_tasks = list(ctx.dispatch_result.get("proposed_tasks") or [])
    if not proposed_tasks:
        return {"status": "skipped"}

    existing = {str(task.get("step_name")) for task in ctx.tasks if task.get("step_name")}
    validated = workflow_definitions.validate_proposed_tasks(
        ctx.skeleton,
        ctx.stage,
        proposed_tasks,
        existing,
    )
    plan = ctx.memory_client.propose_plan(
        ctx.workflow_id,
        str(ctx.task["id"]),
        validated,
    )
    output = dict(ctx.dispatch_result.get("output") or {})
    output["plan_id"] = plan.get("plan_id")

    result: dict[str, Any] = {
        "plan_id": plan.get("plan_id"),
        "output": output,
        "validated_count": len(validated),
    }

    if ctx.stage.plan_approval_required:
        ctx.memory_client.block_task(
            str(ctx.task["id"]),
            f"awaiting_plan_approval:{plan.get('plan_id', '')}",
        )
        result["status"] = "awaiting_plan_approval"
    else:
        result["status"] = "plan_proposed"

    return result
