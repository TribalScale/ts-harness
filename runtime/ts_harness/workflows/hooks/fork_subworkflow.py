"""Fork child workflows from parent stage completion."""

from __future__ import annotations

import uuid
from typing import Any

from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.materialize import load_skeleton, materialize_skeleton_tasks
from ts_harness.workflows.registry import HookContext


def fork_child(
    memory: Any,
    *,
    parent_workflow_id: str,
    tenant_id: str,
    child_type: str,
    intake_input: dict[str, Any],
    actor: str,
    definition_version: str | None = None,
) -> str:
    """Create one child workflow with its skeleton tasks; the intake stage gets ``intake_input``.

    ``definition_version`` pins the child to the version the parent was started
    with; without it the child uses the current definition.
    """
    if definition_version:
        from ts_harness.workflows.engine import get_workflow_engine

        child_skeleton = get_workflow_engine().skeleton(child_type, definition_version=str(definition_version))
    else:
        child_skeleton = load_skeleton(child_type)
    child_id = str(uuid.uuid4())
    memory.create_workflow(
        workflow_id=child_id,
        workflow_type=child_type,
        tenant_id=tenant_id,
        definition_version=child_skeleton.definition_version,
        parent_workflow_id=parent_workflow_id,
        actor=actor,
    )
    task_bodies = materialize_skeleton_tasks(
        child_skeleton,
        workflow_id=child_id,
        tenant_id=tenant_id,
        stage_inputs={child_skeleton.intake_stage_name(): intake_input},
    )
    name_to_id: dict[str, str] = {}
    for body in task_bodies:
        depends_on_names = list(body.get("depends_on") or [])
        payload = {
            "workflow_id": child_id,
            "workflow_type": child_type,
            "tenant_id": tenant_id,
            "step_name": body["step_name"],
            "agent_id": body["agent_id"],
            "input": body.get("input") or {},
            "approval_required": body.get("approval_required", False),
            "max_retries": body.get("max_retries", 0),
            "contract": body.get("contract") or {},
            "definition_version": body.get("definition_version"),
            "lane": body.get("lane"),
            "actor": actor,
        }
        if depends_on_names:
            payload["depends_on"] = [name_to_id[name] for name in depends_on_names if name in name_to_id]
        result = memory.create_task(payload)
        name_to_id[body["step_name"]] = str(result["id"])
    return child_id


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for fork_subworkflow")

    config = dict(ctx.stage.on_complete or {})
    child_type = str(config.get("workflow_type") or "").strip()
    if not child_type:
        raise ValueError("fork_subworkflow requires workflow_type")

    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    items = resolve_value(
        config.get("for_each") or [],
        tasks=tasks,
        workflow=workflow,
    )
    if not isinstance(items, list):
        items = [items] if items is not None else []

    input_map = dict(config.get("input_map") or {})
    child_ids: list[str] = []
    for item in items:
        resolved_input = resolve_value(
            input_map,
            tasks=tasks,
            workflow=workflow,
            extras={"item": item},
        )
        child_ids.append(
            fork_child(
                ctx.memory_client,
                parent_workflow_id=ctx.workflow_id,
                tenant_id=ctx.tenant_id,
                child_type=child_type,
                intake_input=resolved_input if isinstance(resolved_input, dict) else {"item": item},
                actor=ctx.actor or "workflow-runner",
            )
        )

    return {
        "status": "forked",
        "child_workflow_ids": child_ids,
    }
