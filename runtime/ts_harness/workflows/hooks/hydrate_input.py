"""Resolve stage input_map and patch task input."""

from __future__ import annotations

import json
from typing import Any

from ts_harness.workflows.expressions import resolve_input_map
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.registry import HookContext


def _program_artifact_extras(memory_client: Any, workflow: dict[str, Any]) -> dict[str, Any]:
    program_id = workflow.get("program_id")
    if not program_id or not hasattr(memory_client, "get_program"):
        return {}
    try:
        payload = memory_client.get_program(str(program_id))
    except Exception:
        return {}
    program = (payload or {}).get("program") or {}
    metadata = program.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    artifacts = (metadata.get("artifacts") or {}) if isinstance(metadata, dict) else {}
    if not isinstance(artifacts, dict):
        return {}
    extras: dict[str, Any] = {}
    for key in (
        "completed_work_order_ids",
        "work_orders_markdown",
        "work_order_body",
        "next_work_order_id",
        "work_order_id",
        "work_order_ref",
        "repo_url",
        "base_branch",
        "refinery_artifacts",
        "foundry_artifacts",
        "planner_artifacts",
        "pr_url",
        "pr_urls",
        "qa_status",
        "qa_summary",
        "qa_evidence",
        "related_program_context",
    ):
        if artifacts.get(key) is not None:
            extras[key] = artifacts[key]
    if extras.get("work_orders_markdown") and extras.get("next_work_order_id") and not extras.get(
        "work_order_body"
    ):
        from ts_harness.implementation.work_order_context import extract_work_order_section

        body = extract_work_order_section(
            str(extras["work_orders_markdown"]),
            str(extras["next_work_order_id"]),
        )
        if body:
            extras["work_order_body"] = body
    return extras


def _overlay_inflight_task(ctx: HookContext, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge the in-flight task so $prior.<step>.output resolves before DB persist.

    The runner invokes on_complete hydrate before complete_task(), so the
    workflow payload still has the prior (often empty) output. Overlay the
    dispatch result so downstream input_maps see the just-produced artifacts.
    """
    current = dict(ctx.task or {})
    dispatch_output = (ctx.dispatch_result or {}).get("output")
    if isinstance(dispatch_output, dict):
        current["output"] = dict(dispatch_output)
        current["status"] = "succeeded"
    current_id = str(current.get("id") or "")
    current_step = str(current.get("step_name") or "")
    if not current_id and not current_step:
        return list(tasks)

    overlaid: list[dict[str, Any]] = []
    replaced = False
    for task in tasks:
        same_id = current_id and str(task.get("id") or "") == current_id
        same_step = (not current_id) and current_step and str(task.get("step_name") or "") == current_step
        if same_id or same_step:
            merged = {**task, **current}
            if isinstance(dispatch_output, dict):
                merged["output"] = dict(dispatch_output)
                merged["status"] = "succeeded"
            overlaid.append(merged)
            replaced = True
        else:
            overlaid.append(task)
    if not replaced and current:
        overlaid.append(current)
    return overlaid


def _drop_unresolved(resolved: dict[str, Any]) -> dict[str, Any]:
    """Keep existing task input when an expression still resolves to None."""
    return {key: value for key, value in resolved.items() if value is not None}


def _latest_hydratable_task(
    tasks: list[dict[str, Any]],
    step_name: str,
) -> dict[str, Any] | None:
    """Pick the newest active task for a step so revision loops hydrate the new gate."""
    terminal = {"succeeded", "cancelled", "failed"}
    matches = [
        task
        for task in tasks
        if task.get("step_name") == step_name
        and str(task.get("status") or "") not in terminal
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda task: str(task.get("created_at") or ""))[-1]


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for hydrate_input")
    stage = ctx.stage
    if not stage.input_map:
        return {"status": "skipped", "reason": "no input_map"}

    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    tasks = _overlay_inflight_task(ctx, tasks)
    extras = _program_artifact_extras(ctx.memory_client, workflow)
    resolved = _drop_unresolved(
        resolve_input_map(
            dict(stage.input_map),
            tasks=tasks,
            workflow=workflow,
            reject_comments=ctx.reject_comments,
            extras=extras,
        )
    )
    merged = {**(ctx.task.get("input") or {}), **resolved}
    return ctx.memory_client.patch_task_input(str(ctx.task["id"]), merged, actor=ctx.actor)


def fill_missing_inputs(memory_client: Any, task: dict[str, Any], stage: Any) -> dict[str, Any]:
    """Task input with every unset ``input_map`` key resolved, as of dispatch.

    A stage's input_map applies whether or not an upstream ``hydrate_input`` hook
    names it; values already on the task (hydrated or set by a revision) win.
    """
    current = dict(task.get("input") or {})
    input_map = dict(getattr(stage, "input_map", None) or {})
    missing = {key: expr for key, expr in input_map.items() if current.get(key) is None}
    if not missing:
        return current
    workflow, tasks = load_workflow_payload(memory_client, str(task.get("workflow_id") or ""))
    resolved = _drop_unresolved(
        resolve_input_map(
            missing,
            tasks=tasks,
            workflow=workflow,
            extras=_program_artifact_extras(memory_client, workflow),
        )
    )
    return {**current, **resolved}


def hydrate_downstream(ctx: HookContext, downstream: list[str]) -> list[dict[str, Any]]:
    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    tasks = _overlay_inflight_task(ctx, tasks)
    extras = _program_artifact_extras(ctx.memory_client, workflow)
    results: list[dict[str, Any]] = []
    for step_name in downstream:
        target = _latest_hydratable_task(tasks, step_name)
        if target is None:
            continue
        stage = ctx.skeleton.stage(step_name)
        if not stage.input_map:
            continue
        resolved = _drop_unresolved(
            resolve_input_map(
                dict(stage.input_map),
                tasks=tasks,
                workflow=workflow,
                extras=extras,
            )
        )
        merged = {**(target.get("input") or {}), **resolved}
        results.append(
            ctx.memory_client.patch_task_input(str(target["id"]), merged, actor=ctx.actor),
        )
    return results
