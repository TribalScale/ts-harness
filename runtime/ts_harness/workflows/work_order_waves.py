"""Parallel work-order waves: fork one child workflow per ready WO, join when all finish.

``deliver_wave`` forks a ``deliver_work_order`` child per work order in the
wave. Each child runs deliver, PR checks, QA with its revise loop, and the
merge wait. ``join_wave`` waits on ``workflow.child_terminal`` events that
children send when they finish; it succeeds once every child merged and blocks
when any child failed, keeping the merged children's results.
"""

from __future__ import annotations

import logging
from typing import Any

from ts_harness.implementation.work_order_context import extract_work_order_section
from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.hooks.fork_subworkflow import fork_child

logger = logging.getLogger(__name__)

CHILD_EVENT = "workflow.child_terminal"
CHILD_WORKFLOW_TYPE = "deliver_work_order"
SHARED_INPUT_KEYS = (
    "repo_url",
    "base_branch",
    "work_orders_markdown",
    "work_orders_manifest_path",
    "completed_work_order_ids",
    "refinery_artifacts",
    "foundry_artifacts",
    "planner_artifacts",
    "summary",
)


def _task_input(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("input")
    return dict(raw) if isinstance(raw, dict) else {}


def wave_items(task_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Work orders in this wave, from ``work_orders`` or the legacy single ``work_order_id``."""
    items = [dict(item) for item in task_input.get("work_orders") or [] if isinstance(item, dict) and item.get("work_order_id")]
    if not items and task_input.get("work_order_id"):
        items = [
            {
                "work_order_id": task_input["work_order_id"],
                "work_order_ref": task_input.get("work_order_ref") or task_input["work_order_id"],
                "work_order_body": task_input.get("work_order_body") or "",
            }
        ]
    markdown = str(task_input.get("work_orders_markdown") or "")
    for item in items:
        item.setdefault("work_order_ref", item["work_order_id"])
        if not item.get("work_order_body") and markdown:
            item["work_order_body"] = extract_work_order_section(markdown, str(item["work_order_id"]))
    return items


def fork_wave(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """System step: start one child workflow per work order in the wave."""
    workflow_id = str(task.get("workflow_id") or "")
    _, tasks = load_workflow_payload(memory, workflow_id)
    stored = next((item for item in tasks if str(item.get("id")) == str(task.get("id"))), {})
    task_input = {**_task_input(stored), **_task_input(task)}
    items = wave_items(task_input)
    if not items:
        raise ValueError("deliver_wave has no work orders to deliver")
    shared = {key: task_input[key] for key in SHARED_INPUT_KEYS if task_input.get(key) is not None}
    child_type = str(config.get("workflow_type") or CHILD_WORKFLOW_TYPE)
    children: list[dict[str, str]] = []
    for item in items:
        child_id = fork_child(
            memory,
            parent_workflow_id=workflow_id,
            tenant_id=str(task.get("tenant_id") or "default"),
            child_type=child_type,
            intake_input={**shared, **item},
            actor=actor,
            definition_version=task_input.get("_child_version"),
        )
        children.append({"workflow_id": child_id, "work_order_id": str(item["work_order_id"])})
    return {
        "child_workflow_ids": [child["workflow_id"] for child in children],
        "wave_work_order_ids": [child["work_order_id"] for child in children],
        "children": children,
    }


def _compensate_steps(workflow_type: str) -> set[str]:
    try:
        from ts_harness.workflows.materialize import load_skeleton

        return {stage.name for stage in load_skeleton(workflow_type).stages if stage.kind == "compensate"}
    except Exception:
        return set()


def child_state(workflow: dict[str, Any], tasks: list[dict[str, Any]]) -> str | None:
    """``succeeded``, ``failed`` or ``None`` while the child is still running.

    Only the latest task per step counts, so superseded revise-loop tasks are ignored.
    """
    skip = _compensate_steps(str(workflow.get("workflow_type") or CHILD_WORKFLOW_TYPE))
    latest: dict[str, dict[str, Any]] = {}
    for task in tasks:
        step = str(task.get("step_name") or "")
        if step and step not in skip:
            latest[step] = task
    if not latest:
        return None
    statuses = {str(task.get("status") or "") for task in latest.values()}
    if "failed" in statuses:
        return "failed"
    if statuses <= {"succeeded", "cancelled"}:
        return "succeeded"
    return None


def _merged_output(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for task in tasks:
        if task.get("status") == "succeeded" and isinstance(task.get("output"), dict):
            merged.update(task["output"])
    return merged


def child_label(child: dict[str, str]) -> str:
    return str(child.get("label") or child.get("work_order_id") or child.get("workflow_id") or "")


def wave_result(memory: Any, children: list[dict[str, str]]) -> dict[str, Any]:
    """Aggregate a wave: ``pending`` until every child finished, then ``succeeded`` or ``failed``.

    ``children_outputs`` maps each finished child's label to its merged task output,
    so joins over non-delivery children can read what the children produced.
    """
    prs: list[dict[str, Any]] = []
    failed: list[str] = []
    failed_labels: list[str] = []
    pending: list[str] = []
    children_outputs: dict[str, dict[str, Any]] = {}
    latest_merge: tuple[str, str] = ("", "")
    for child in children:
        workflow, tasks = load_workflow_payload(memory, child["workflow_id"])
        state = child_state(workflow, tasks)
        work_order_id = child.get("work_order_id") or ""
        label = child_label(child)
        if state is None:
            pending.append(work_order_id or label)
            continue
        if state == "failed":
            failed.append(work_order_id)
            failed_labels.append(label)
            continue
        output = _merged_output(tasks)
        children_outputs[label] = output
        merged_at = max(
            (str(task.get("completed_at") or "") for task in tasks if task.get("step_name") == "await_human_merge"),
            default="",
        )
        merge_sha = str(output.get("merge_sha") or "")
        if merge_sha and merged_at >= latest_merge[0]:
            latest_merge = (merged_at, merge_sha)
        prs.append(
            {
                "work_order_id": work_order_id,
                "workflow_id": child["workflow_id"],
                "pr_url": output.get("pr_url"),
                "merge_sha": merge_sha or None,
            }
        )
    output = {
        "wave_work_order_ids": [pr["work_order_id"] for pr in prs],
        "pr_urls": sorted({str(pr["pr_url"]) for pr in prs if pr.get("pr_url")}),
        "prs": prs,
        "failed_work_order_ids": failed,
        "children_outputs": children_outputs,
        "failed_labels": failed_labels,
    }
    if latest_merge[1]:
        output["merge_sha"] = latest_merge[1]
    if output["pr_urls"]:
        output["pr_url"] = output["pr_urls"][0]
    if pending:
        return {"state": "pending", "pending": pending, "output": output}
    return {"state": "failed" if failed else "succeeded", "output": output}


def _wave_children(spec: dict[str, Any], *, tasks: list[dict[str, Any]], workflow: dict[str, Any]) -> list[dict[str, str]]:
    resolved = resolve_value(spec.get("source"), tasks=tasks, workflow=workflow)
    children: list[dict[str, str]] = []
    for item in resolved if isinstance(resolved, list) else []:
        if isinstance(item, dict) and item.get("workflow_id"):
            child = {"workflow_id": str(item["workflow_id"]), "work_order_id": str(item.get("work_order_id") or "")}
            if item.get("label") not in (None, ""):
                child["label"] = str(item["label"])
            children.append(child)
        elif isinstance(item, str) and item:
            children.append({"workflow_id": item, "work_order_id": ""})
    return children


def prepare_children_wait(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    actor: str = "workflow-runner",
) -> dict[str, Any]:
    """Pin the join to the wave's children; ``ready`` when they all finished already."""
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    children = _wave_children(dict(config.get("children") or {}), tasks=tasks, workflow=workflow)
    if not children:
        raise ValueError("join_wave resolved no child workflows")
    options = {"children": children}
    memory.patch_task_input(
        str(task["id"]),
        {**_task_input(task), "_wait_match": {}, "_wait_options": options},
        actor=actor,
    )
    result: dict[str, Any] = {"match": {}, "options": options}
    aggregate = wave_result(memory, children)
    if aggregate["state"] != "pending":
        result["ready"] = {"wave_state": aggregate["state"]}
    return result


def pinned_children(task: dict[str, Any]) -> list[dict[str, str]]:
    options = _task_input(task).get("_wait_options") or {}
    return [dict(child) for child in options.get("children") or [] if isinstance(child, dict)]


def notify_parent(memory: Any, workflow: dict[str, Any], state: str, *, actor: str = "workflow-runner") -> dict[str, Any] | None:
    """Tell the parent's join that this child finished; a parent without a join ignores it."""
    parent_id = str(workflow.get("parent_workflow_id") or "").strip()
    if not parent_id:
        return None
    from ts_harness.workflows.engine import get_workflow_engine

    payload = {"child_workflow_id": str(workflow.get("id") or ""), "state": state}
    try:
        return get_workflow_engine().resume_external(
            parent_id,
            event_type=CHILD_EVENT,
            payload=payload,
            actor=actor,
            tenant_id=str(workflow.get("tenant_id") or "default"),
        )
    except ValueError as exc:
        logger.info("parent %s has no join waiting for child %s: %s", parent_id, payload["child_workflow_id"], exc)
        return None
