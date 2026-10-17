"""Complete a delivery program phase when all workflow tasks succeed."""

from __future__ import annotations

from typing import Any

from ts_harness.implementation.work_order_context import (
    parse_work_order_dependencies,
    select_next_work_order_id,
)
from ts_harness.workflows.delivery_program import get_delivery_program_service
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.program_brief import publish_program_brief
from ts_harness.workflows.program_materialize import (
    automatic_edges_from,
    next_list_item,
    node_is_terminal,
    parse_program_snapshot,
    reentry_item_key,
    reentry_keys,
    reentry_max_iterations,
    reentry_mode,
    snapshot_node,
)
from ts_harness.workflows.registry import HookContext
from ts_harness.repo_paths import ensure_scripts_importable

ensure_scripts_importable()
from program_legacy_compat import frozen_legacy_compatibility_topology  # noqa: E402


def handle(ctx: HookContext) -> dict[str, Any]:
    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    program_id = workflow.get("program_id")
    if not program_id and workflow.get("parent_workflow_id"):
        from ts_harness.workflows.work_order_waves import child_state, notify_parent

        state = child_state(workflow, tasks)
        if state is None:
            return {"status": "skipped", "reason": "child workflow still running"}
        return {"status": "child_finished", "state": state, "parent": notify_parent(ctx.memory_client, workflow, state)}
    if not program_id:
        return {"status": "skipped", "reason": "workflow not linked to program"}

    if not tasks:
        return {"status": "skipped", "reason": "no tasks"}

    terminal_statuses = {"succeeded", "cancelled"}
    active = [
        task
        for task in tasks
        if str(task.get("status") or "") not in terminal_statuses
    ]
    if active:
        return {"status": "skipped", "reason": "workflow still has active tasks"}

    failed = [task for task in tasks if task.get("status") == "failed"]
    if failed:
        return {"status": "phase_failed", "program_id": str(program_id)}

    service = get_delivery_program_service()
    result = service.on_workflow_terminal(
        ctx.workflow_id,
        actor=ctx.actor or "workflow-runner",
    )
    program_cfg = dict(getattr(ctx.skeleton, "program", None) or {})
    program_phase = str(workflow.get("program_phase") or "")

    program_payload = ctx.memory_client.get_program(str(program_id))
    program = program_payload.get("program") or {}
    snapshot = parse_program_snapshot(program.get("program_definition_snapshot"))
    node_cfg = _node_config(snapshot, program_phase)
    reentry = node_cfg.get("reentry") if isinstance(node_cfg.get("reentry"), dict) else None
    complete_channel_text = str(node_cfg.get("complete_channel_text") or "").strip()

    if reentry:
        # Re-read after on_workflow_terminal may have appended completed list artifacts.
        program_payload = ctx.memory_client.get_program(str(program_id))
        program = program_payload.get("program") or {}
        metadata = program.get("metadata") or {}
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)
        artifacts = dict(
            (metadata.get("artifacts") or {}) if isinstance(metadata, dict) else {}
        )

        loop_mode = reentry_mode(reentry)
        if loop_mode == "list":
            looped = _advance_list_reentry(
                ctx, service, workflow, program, program_phase, artifacts, reentry, result
            )
            if looped is not None:
                return looped
            wave = []
        else:
            wave = _next_reentry_wave(artifacts, reentry)
        next_item = wave[0] if wave else None
        target_node = str(reentry.get("target_node") or program_phase or "").strip()
        next_item_key = str(
            reentry.get("next_item_artifact") or "next_work_order_id"
        ).strip()
        if next_item and target_node and reentry_max_iterations(reentry) is not None:
            count, iterations = _bump_iterations(artifacts, program_phase)
            ctx.memory_client.patch_program_artifacts(
                str(program_id),
                {"reentry_iterations": iterations},
                merge=True,
                actor=ctx.actor or "workflow-runner",
            )
            if count >= int(reentry_max_iterations(reentry) or 0):
                return _cap_reentry(ctx, workflow, program, program_phase, count, result)
        if next_item and target_node:
            reentry_result = service.advance_program(
                program_id=str(program_id),
                target_phase=target_node,
                summary=str(workflow.get("summary") or ""),
                artifacts={
                    next_item_key: next_item,
                    "next_work_order_ids": wave,
                    "work_orders_handoff": True,
                    "planning_track": "work_orders",
                },
                tenant_id=ctx.tenant_id,
                binding_key=workflow.get("source_channel"),
                source_workflow_id=ctx.workflow_id,
                actor=ctx.actor or "workflow-runner",
            )
            next_workflow_id = str((reentry_result or {}).get("workflow_id") or "") or None
            try:
                ctx.memory_client.complete_workflow(
                    ctx.workflow_id,
                    actor=ctx.actor or "workflow-runner",
                    reason="phase_complete",
                    superseded_by=next_workflow_id,
                )
            except Exception:
                pass
            return _with_brief(
                ctx,
                program_id,
                program,
                program_phase,
                {
                    "status": "phase_reentered_implement",
                    "program_id": str(program_id),
                    "advance": reentry_result,
                    "artifacts": result,
                },
            )

        if loop_mode == "work_orders" and _has_remaining_items(artifacts, reentry):
            return {
                "status": "phase_blocked_unmet_dependencies",
                "program_id": str(program_id),
                "artifacts": result,
                "reason": "remaining work orders have unmet dependencies",
            }

        loop_done = (
            {"work_orders_delivery_complete": True}
            if loop_mode == "work_orders"
            else {f"{program_phase}_loop_complete": True}
        )
        auto_edges = automatic_edges_from(snapshot, program_phase) if snapshot is not None and program_phase else []
        if len(auto_edges) == 1:
            ctx.memory_client.patch_program_artifacts(
                str(program_id),
                loop_done,
                merge=True,
                actor=ctx.actor or "workflow-runner",
            )
            if complete_channel_text:
                _notify_channel(ctx, workflow, complete_channel_text)
            return _auto_advance(
                ctx, service, workflow, program, snapshot, program_phase, auto_edges[0], result
            )

        # All reentry items delivered and nothing follows — the program is done.
        ctx.memory_client.patch_program_artifacts(
            str(program_id),
            {
                **loop_done,
                "program_status": "succeeded",
                "completed_at_phase": program_phase or "implement",
            },
            merge=True,
            actor=ctx.actor or "workflow-runner",
        )
        _resume_parent_pending_advance(ctx, service, program)
        try:
            ctx.memory_client.complete_workflow(
                ctx.workflow_id,
                actor=ctx.actor or "workflow-runner",
                reason="phase_complete",
            )
        except Exception:
            pass
        if workflow:
            ctx.workflow = {**(ctx.workflow or {}), **workflow}
        if complete_channel_text:
            ctx.extras["channel_text"] = complete_channel_text
            ctx.runner_event = "completed"
            try:
                from ts_harness.workflows.hooks.notify_bound_channel import (
                    handle as notify_bound,
                )

                notify_bound(ctx)
            except Exception:
                pass
        return _with_brief(
            ctx,
            program_id,
            program,
            program_phase,
            {
                "status": (
                    "phase_completed_work_orders_done"
                    if loop_mode == "work_orders"
                    else "phase_completed_loop_done"
                ),
                "program_id": str(program_id),
                "artifacts": result,
            },
        )

    # Snapshot-driven automatic transitions are the sole topology source for
    # program-linked runs. Skeleton program/bridge blocks remain legacy hints.
    if snapshot is not None and program_phase:
        auto_edges = automatic_edges_from(snapshot, program_phase)
        if len(auto_edges) == 1:
            return _auto_advance(
                ctx, service, workflow, program, snapshot, program_phase, auto_edges[0], result
            )
        if node_is_terminal(snapshot, program_phase):
            ctx.memory_client.patch_program_artifacts(
                str(program_id),
                {"program_status": "succeeded"},
                merge=True,
                actor=ctx.actor or "workflow-runner",
            )
            _resume_parent_pending_advance(ctx, service, program)
            if complete_channel_text:
                ctx.extras["channel_text"] = complete_channel_text
                ctx.runner_event = "completed"
                try:
                    from ts_harness.workflows.hooks.notify_bound_channel import (
                        handle as notify_bound,
                    )

                    notify_bound(ctx)
                except Exception:
                    pass
            return _with_brief(
                ctx,
                program_id,
                program,
                program_phase,
                {
                    "status": "phase_completed",
                    "program_id": str(program_id),
                    "artifacts": result,
                },
            )
        return _with_brief(
            ctx,
            program_id,
            program,
            program_phase,
            {
                "status": "phase_completed",
                "program_id": str(program_id),
                "artifacts": result,
            },
        )

    if program_cfg.get("auto_advance") and program_cfg.get("next_phase"):
        advance = service.advance_program(
            program_id=str(program_id),
            target_phase=str(program_cfg.get("next_phase")),
            summary=str(workflow.get("summary") or ""),
            tenant_id=ctx.tenant_id,
            binding_key=workflow.get("source_channel"),
            source_workflow_id=ctx.workflow_id,
            actor=ctx.actor or "workflow-runner",
        )
        next_workflow_id = str((advance or {}).get("workflow_id") or "") or None
        try:
            ctx.memory_client.complete_workflow(
                ctx.workflow_id,
                actor=ctx.actor or "workflow-runner",
                reason="phase_complete",
                superseded_by=next_workflow_id,
            )
        except Exception:
            # Workflow may already be terminal; advance still succeeded.
            pass
        return _with_brief(
            ctx,
            program_id,
            program,
            program_phase,
            {
                "status": "phase_completed_auto_advanced",
                "program_id": str(program_id),
                "advance": advance,
                "artifacts": result,
            },
        )

    if program_cfg.get("terminal"):
        ctx.memory_client.patch_program_artifacts(
            str(program_id),
            {"program_status": "succeeded"},
            merge=True,
            actor=ctx.actor or "workflow-runner",
        )

    return _with_brief(
        ctx,
        program_id,
        program,
        program_phase,
        {
            "status": "phase_completed",
            "program_id": str(program_id),
            "artifacts": result,
        },
    )


CHILD_DONE_STATUSES = frozenset({"succeeded", "completed", "cancelled"})


def _unfinished_children(memory: Any, program_id: str, *, exclude: str | None = None) -> list[str]:
    """Child programs that have not finished; fails closed when status is unavailable."""
    try:
        status = memory.get_program_status(program_id) or {}
    except Exception:
        return ["status unavailable"]
    return [
        str(child.get("id"))
        for child in status.get("children") or []
        if str(child.get("id")) != str(exclude or "")
        and str(child.get("status") or "") not in CHILD_DONE_STATUSES
    ]


def _notify_channel(ctx: HookContext, workflow: dict[str, Any], text: str) -> None:
    if workflow:
        ctx.workflow = {**(ctx.workflow or {}), **workflow}
    ctx.extras["channel_text"] = text
    ctx.runner_event = "completed"
    try:
        from ts_harness.workflows.hooks.notify_bound_channel import handle as notify_bound

        notify_bound(ctx)
    except Exception:
        pass


def _advance(
    ctx: HookContext,
    service: Any,
    *,
    program_id: str,
    target: str,
    summary: str,
    artifacts: dict[str, Any] | None,
    binding_key: Any,
) -> dict[str, Any]:
    advance = service.advance_program(
        program_id=program_id,
        target_phase=target,
        summary=summary,
        artifacts=artifacts,
        tenant_id=ctx.tenant_id,
        binding_key=binding_key,
        source_workflow_id=ctx.workflow_id,
        actor=ctx.actor or "workflow-runner",
    )
    next_workflow_id = str((advance or {}).get("workflow_id") or "") or None
    try:
        ctx.memory_client.complete_workflow(
            ctx.workflow_id,
            actor=ctx.actor or "workflow-runner",
            reason="phase_complete",
            superseded_by=next_workflow_id,
        )
    except Exception:
        pass
    return advance


def _auto_advance(
    ctx: HookContext,
    service: Any,
    workflow: dict[str, Any],
    program: dict[str, Any],
    snapshot: dict[str, Any] | None,
    program_phase: str,
    edge: dict[str, Any],
    result: Any,
) -> dict[str, Any]:
    program_id = str(workflow.get("program_id"))
    target = str(edge.get("target") or "")
    artifact_patch = edge.get("artifact_patch")
    advance_artifacts = dict(artifact_patch) if isinstance(artifact_patch, dict) else None
    if _node_config(snapshot, target).get("await_children"):
        waiting_on = _unfinished_children(ctx.memory_client, program_id)
        if waiting_on:
            ctx.memory_client.patch_program_artifacts(
                program_id,
                {
                    "pending_auto_advance": {
                        "target": target,
                        "source_phase": program_phase,
                        "artifacts": advance_artifacts or {},
                        "summary": str(workflow.get("summary") or ""),
                        "binding_key": workflow.get("source_channel"),
                    }
                },
                merge=True,
                actor=ctx.actor or "workflow-runner",
            )
            try:
                ctx.memory_client.complete_workflow(
                    ctx.workflow_id,
                    actor=ctx.actor or "workflow-runner",
                    reason="phase_complete",
                )
            except Exception:
                pass
            return _with_brief(
                ctx,
                program_id,
                program,
                program_phase,
                {
                    "status": "phase_waiting_on_children",
                    "program_id": program_id,
                    "target": target,
                    "waiting_on": waiting_on,
                    "artifacts": result,
                },
            )
    advance = _advance(
        ctx,
        service,
        program_id=program_id,
        target=target,
        summary=str(workflow.get("summary") or ""),
        artifacts=advance_artifacts,
        binding_key=workflow.get("source_channel"),
    )
    return _with_brief(
        ctx,
        program_id,
        program,
        program_phase,
        {
            "status": "phase_completed_auto_advanced",
            "program_id": program_id,
            "advance": advance,
            "artifacts": result,
        },
    )


def _program_artifacts(program: dict[str, Any]) -> dict[str, Any]:
    metadata = program.get("metadata") or {}
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    return dict((metadata.get("artifacts") or {}) if isinstance(metadata, dict) else {})


def _resume_parent_pending_advance(ctx: HookContext, service: Any, program: dict[str, Any]) -> dict[str, Any] | None:
    """When the last child finishes, run the parent's held automatic advance (e.g. release)."""
    parent_id = str(program.get("parent_program_id") or "").strip()
    if not parent_id:
        return None
    try:
        parent = ctx.memory_client.get_program(parent_id).get("program") or {}
    except Exception:
        return None
    pending = _program_artifacts(parent).get("pending_auto_advance")
    if not isinstance(pending, dict) or not pending.get("target"):
        return None
    if _unfinished_children(ctx.memory_client, parent_id, exclude=str(program.get("id") or "")):
        return None
    ctx.memory_client.patch_program_artifacts(
        parent_id,
        {"pending_auto_advance": None},
        merge=True,
        actor=ctx.actor or "workflow-runner",
    )
    return service.advance_program(
        program_id=parent_id,
        target_phase=str(pending["target"]),
        summary=str(pending.get("summary") or parent.get("summary") or ""),
        artifacts=dict(pending.get("artifacts") or {}) or None,
        tenant_id=str(parent.get("tenant_id") or ctx.tenant_id or "default"),
        binding_key=pending.get("binding_key"),
        source_workflow_id=ctx.workflow_id,
        actor=ctx.actor or "workflow-runner",
    )


def _try_publish_brief(
    ctx: HookContext,
    program_id: Any,
    program: dict[str, Any],
    phase: str,
) -> str:
    try:
        metadata = program.get("metadata") or {}
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)
        artifacts = dict((metadata.get("artifacts") or {}) if isinstance(metadata, dict) else {})
        publish_program_brief(
            ctx.memory_client,
            program_id=str(program_id),
            tenant_id=str(ctx.tenant_id or program.get("tenant_id") or "default"),
            project_id=program.get("project_id"),
            phase=str(phase or program.get("current_phase") or ""),
            artifacts=artifacts,
            actor=ctx.actor or "workflow-runner",
        )
        return "ok"
    except Exception:
        return "failed"


def _with_brief(
    ctx: HookContext,
    program_id: Any,
    program: dict[str, Any],
    phase: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    payload["brief_publish"] = _try_publish_brief(ctx, program_id, program, phase)
    return payload


def _bump_iterations(artifacts: dict[str, Any], node_id: str) -> tuple[int, dict[str, Any]]:
    iterations = dict(artifacts.get("reentry_iterations") or {})
    count = int(iterations.get(node_id) or 0) + 1
    iterations[node_id] = count
    return count, iterations


def _cap_reentry(
    ctx: HookContext,
    workflow: dict[str, Any],
    program: dict[str, Any],
    program_phase: str,
    count: int,
    result: Any,
) -> dict[str, Any]:
    """Stop a loop that hit max_iterations; cancelling frees the schedule and drops Inbox items."""
    program_id = str(program.get("id") or workflow.get("program_id") or "")
    actor = ctx.actor or "workflow-runner"
    try:
        ctx.memory_client.complete_workflow(ctx.workflow_id, actor=actor, reason="phase_complete")
    except Exception:
        pass
    ctx.memory_client.cancel_program(program_id, actor=actor, reason="reentry_max_iterations")
    _notify_channel(
        ctx,
        workflow,
        f"Stopped the `{program_phase}` loop after {count} iterations (max_iterations reached). "
        "The program was cancelled.",
    )
    return {
        "status": "phase_reentry_capped",
        "program_id": program_id,
        "node": program_phase,
        "iterations": count,
        "artifacts": result,
    }


def _advance_list_reentry(
    ctx: HookContext,
    service: Any,
    workflow: dict[str, Any],
    program: dict[str, Any],
    program_phase: str,
    artifacts: dict[str, Any],
    reentry: dict[str, Any],
    result: Any,
) -> dict[str, Any] | None:
    """Record the finished item and reenter with the next one; ``None`` when the list is done."""
    program_id = str(program.get("id") or workflow.get("program_id") or "")
    actor = ctx.actor or "workflow-runner"
    _, completed_key, next_key = reentry_keys(reentry)
    completed = list(artifacts.get(completed_key) or [])
    current = artifacts.get(next_key)
    if current is not None:
        key = reentry_item_key(current)
        if key not in {reentry_item_key(item) for item in completed}:
            completed.append(key)
    count, iterations = _bump_iterations(artifacts, program_phase)
    patch = {completed_key: completed, "reentry_iterations": iterations}
    ctx.memory_client.patch_program_artifacts(program_id, patch, merge=True, actor=actor)
    artifacts = {**artifacts, **patch}

    next_item = next_list_item(artifacts, reentry)
    if next_item is None:
        return None
    cap = reentry_max_iterations(reentry)
    if cap is not None and count >= cap:
        return _cap_reentry(ctx, workflow, program, program_phase, count, result)

    reentry_result = service.advance_program(
        program_id=program_id,
        target_phase=program_phase,
        summary=str(workflow.get("summary") or ""),
        artifacts={next_key: next_item, "reentry_index": count},
        tenant_id=ctx.tenant_id,
        binding_key=workflow.get("source_channel"),
        source_workflow_id=ctx.workflow_id,
        actor=actor,
    )
    next_workflow_id = str((reentry_result or {}).get("workflow_id") or "") or None
    try:
        ctx.memory_client.complete_workflow(
            ctx.workflow_id,
            actor=actor,
            reason="phase_complete",
            superseded_by=next_workflow_id,
        )
    except Exception:
        pass
    return _with_brief(
        ctx,
        program_id,
        program,
        program_phase,
        {
            "status": "phase_reentered",
            "node": program_phase,
            "program_id": program_id,
            "iteration": count,
            "advance": reentry_result,
            "artifacts": result,
        },
    )


def _node_config(
    snapshot: dict[str, Any] | None,
    program_phase: str,
) -> dict[str, Any]:
    if not program_phase:
        return {}
    effective = snapshot if snapshot is not None else frozen_legacy_compatibility_topology()
    try:
        node = snapshot_node(effective, program_phase)
    except KeyError:
        return {}
    return dict(node) if isinstance(node, dict) else {}


def _next_reentry_item(
    artifacts: dict[str, Any],
    reentry: dict[str, Any],
) -> str | None:
    remaining_key = str(reentry.get("remaining_list_artifact") or "work_order_ids")
    completed_key = str(
        reentry.get("completed_list_artifact") or "completed_work_order_ids"
    )
    all_ids = list(artifacts.get(remaining_key) or [])
    completed = list(artifacts.get(completed_key) or [])
    structured = artifacts.get("work_order_dependencies")
    dependencies: dict[str, list[str]] = {}
    if isinstance(structured, dict):
        dependencies = {
            str(k): [str(d) for d in (v or [])]
            for k, v in structured.items()
        }
    elif artifacts.get("work_orders_markdown"):
        dependencies = parse_work_order_dependencies(str(artifacts["work_orders_markdown"]))
    return select_next_work_order_id(all_ids, completed, dependencies)


def _next_reentry_wave(
    artifacts: dict[str, Any],
    reentry: dict[str, Any],
) -> list[str]:
    """Every ready item up to ``delivery.max_parallel_work_orders``; the first is the serial next item."""
    from ts_harness.implementation.work_order_context import (
        dependencies_from_artifacts,
        max_parallel_work_orders,
        select_ready_work_order_ids,
    )
    from ts_harness.workflows.program_adapters import _live_program_defaults

    remaining_key = str(reentry.get("remaining_list_artifact") or "work_order_ids")
    completed_key = str(
        reentry.get("completed_list_artifact") or "completed_work_order_ids"
    )
    return select_ready_work_order_ids(
        list(artifacts.get(remaining_key) or []),
        list(artifacts.get(completed_key) or []),
        dependencies_from_artifacts(artifacts),
        max_parallel=max_parallel_work_orders(_live_program_defaults()),
    )


def _has_remaining_items(
    artifacts: dict[str, Any],
    reentry: dict[str, Any],
) -> bool:
    remaining_key = str(reentry.get("remaining_list_artifact") or "work_order_ids")
    completed_key = str(
        reentry.get("completed_list_artifact") or "completed_work_order_ids"
    )
    all_ids = {str(item) for item in list(artifacts.get(remaining_key) or [])}
    completed = {str(item) for item in list(artifacts.get(completed_key) or [])}
    return bool(all_ids - completed)


def _next_implementable_work_order(artifacts: dict[str, Any]) -> str | None:
    """Backward-compatible helper for tests and callers."""
    return _next_reentry_item(
        artifacts,
        {
            "remaining_list_artifact": "work_order_ids",
            "completed_list_artifact": "completed_work_order_ids",
        },
    )
