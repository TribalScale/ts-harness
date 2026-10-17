"""Mark workflow artifact approved and prepare optional bridge opt-in."""

from __future__ import annotations

from typing import Any

from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.session_compat import (
    write_approved_artifact,
    write_session_mode,
    write_source_workflow_id,
    write_workflow_phase,
)
from ts_harness.workflows.artifacts import artifact_steps, latest_artifact_output
from ts_harness.workflows.workflow_session import get_phase_tracking


def _session_for_workflow(workflow_id: str, tenant_id: str) -> SessionRecord | None:
    for session in get_session_store().list_for_tenant(tenant_id):
        metadata = session.metadata or {}
        if metadata.get("workflow_id") == workflow_id:
            return session
    return None


def handle(ctx: HookContext) -> dict[str, Any]:
    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    steps = artifact_steps(ctx.skeleton)
    artifact, _ = latest_artifact_output(tasks, steps)
    if artifact is None:
        raise ValueError("no succeeded artifact task found to approve")

    session = ctx.extras.get("session")
    if session is None:
        session = _session_for_workflow(ctx.workflow_id, ctx.tenant_id)

    tracking = get_phase_tracking(ctx.skeleton)
    approved_value = tracking.approved_value if tracking else "approved"

    if session is not None:
        metadata = dict(session.metadata or {})
        metadata = write_session_mode(metadata, "workflow_linked")
        metadata.update(
            {
                "workflow_type": ctx.workflow_type,
            },
        )
        metadata = write_workflow_phase(metadata, approved_value)
        metadata = write_approved_artifact(metadata, artifact)
        metadata = write_source_workflow_id(metadata, ctx.workflow_id)
        session.metadata = metadata
        get_session_store().save(session)

    bridge = dict(getattr(ctx.skeleton, "bridge", None) or {})
    runner_event = "awaiting_bridge_opt_in" if bridge else "completed"
    ctx.runner_event = runner_event
    ctx.workflow = workflow
    try:
        from ts_harness.workflows.hooks import notify_bound_channel

        notify_bound_channel.handle(ctx)
    except Exception:
        pass

    target = str(bridge.get("target_workflow_type") or "")
    message = (
        f"Artifact approved. Offer bridge_workflow to start {target}."
        if target
        else "Artifact approved."
    )
    return {
        "status": "artifact_approved",
        "workflow_id": ctx.workflow_id,
        "approved_artifact": artifact,
        "message": message,
    }
