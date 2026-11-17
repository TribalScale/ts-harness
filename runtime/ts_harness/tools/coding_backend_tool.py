"""Agent tool: run_coding_backend (host OpenCode implement)."""

from __future__ import annotations

from typing import Any

from ts_harness.implementation.coding_job import (
    CodingRequestError,
    binding_from_task_input,
    build_coding_request,
    record_coding_refs,
    run_coding_request,
)
from ts_harness.tools.policy import ToolPolicyContext

__all__ = ["delivery_binding", "handle_run_coding_backend", "record_coding_refs"]


def delivery_binding(ctx: ToolPolicyContext, *, client: Any = None) -> dict[str, Any]:
    """Repo, branch, PR, work order and revision feedback stored on this workflow's deliver task.

    A revision must not follow the agent's default repository when the work order named one,
    and the agent's retyped copy of the feedback can be corrupted (e.g. a NUL from a broken
    ``\\u00e9`` escape), so the stored task input wins.
    """
    workflow_id = str(getattr(ctx, "linked_workflow_id", None) or "").strip()
    if not workflow_id:
        return {}
    if client is None:
        try:
            from ts_harness.memory.client import get_memory_client

            client = get_memory_client()
        except Exception:
            return {}
    try:
        payload = client.get_workflow(workflow_id)
    except Exception:
        return {}
    chosen: dict[str, Any] = {}
    for task in payload.get("tasks") or []:
        if str(task.get("step_name") or "") != "deliver_work_order":
            continue
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        if str(task_input.get("repo_url") or "").strip():
            chosen = task_input
    return binding_from_task_input(chosen)


def handle_run_coding_backend(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    if ctx.agent_id != "dev-agent":
        return {"ok": False, "error": "run_coding_backend is restricted to dev-agent"}
    try:
        spec = build_coding_request(
            args,
            delivery_workflow_id=str(getattr(ctx, "linked_workflow_id", None) or "").strip() or None,
            program_id=str(args.get("program_id") or "").strip() or None,
            requested_by=str(args.get("requested_by") or ctx.actor_email or ""),
            bound=delivery_binding(ctx),
            agent_id=ctx.agent_id,
        )
    except CodingRequestError as exc:
        return {"ok": False, "error": str(exc), **exc.extra}
    return run_coding_request(spec)
