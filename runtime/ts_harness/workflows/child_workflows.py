"""``fork_children`` system step: start child workflows of any type from a barrier stage.

The join is a following stage with ``wait_external`` on ``workflow.child_terminal``
and ``children: {source: "$prior.<fork stage>.output.children"}`` — the same
join the work-order waves use.
"""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.hooks.fork_subworkflow import fork_child


def _task_input(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("input")
    return dict(raw) if isinstance(raw, dict) else {}


def fork_children(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Start one child per ``for_each`` item (or a single child) and return them for the join."""
    child_type = str(config.get("workflow_type") or "").strip()
    if not child_type:
        raise ValueError("fork_children requires workflow_type")
    workflow_id = str(task.get("workflow_id") or "")
    workflow, tasks = load_workflow_payload(memory, workflow_id)
    stored = next((item for item in tasks if str(item.get("id")) == str(task.get("id"))), {})
    task_input = {**_task_input(stored), **_task_input(task)}

    if "for_each" in config:
        items = resolve_value(config.get("for_each"), tasks=tasks, workflow=workflow)
        if not isinstance(items, list):
            items = [items] if items is not None else []
        if not items:
            raise ValueError("fork_children resolved no items to fork")
    else:
        items = [None]

    labels: list[str] = []
    for index, item in enumerate(items):
        label_expr = config.get("label")
        label = (
            resolve_value(label_expr, tasks=tasks, workflow=workflow, extras={"item": item})
            if label_expr
            else index
        )
        labels.append(str(label if label not in (None, "") else index))
    if len(set(labels)) != len(labels):
        raise ValueError("fork_children labels must be unique per child")

    children = [
        {"workflow_id": str(child["workflow_id"]), "label": str(child["label"])}
        for child in task_input.get("_forked_children") or []
        if isinstance(child, dict) and child.get("workflow_id")
    ]
    already = {child["label"] for child in children}
    input_map = dict(config.get("input_map") or {})
    for item, label in zip(items, labels, strict=True):
        if label in already:
            continue
        intake = resolve_value(input_map, tasks=tasks, workflow=workflow, extras={"item": item})
        child_id = fork_child(
            memory,
            parent_workflow_id=workflow_id,
            tenant_id=str(task.get("tenant_id") or "default"),
            child_type=child_type,
            intake_input=intake if isinstance(intake, dict) and intake else {"item": item},
            actor=actor,
            definition_version=task_input.get("_child_version"),
        )
        children.append({"workflow_id": child_id, "label": label})
        task_input["_forked_children"] = list(children)
        memory.patch_task_input(str(task.get("id")), task_input, actor=actor)

    return {
        "children": children,
        "child_workflow_ids": [child["workflow_id"] for child in children],
        "child_workflow_type": child_type,
    }
