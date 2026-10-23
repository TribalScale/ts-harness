"""Bridge an approved source workflow into a downstream workflow."""

from __future__ import annotations

from typing import Any
import uuid

from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.workflows.delivery_program import (
    get_delivery_program_service,
    planning_track_for_workflow_type,
    phase_for_workflow_type,
    resolve_bridge_target_from_text,
)
from ts_harness.workflows.materialize import load_skeleton, materialize_skeleton_tasks
from ts_harness.workflows.program_materialize import (
    node_id_for_workflow_type,
    parse_program_snapshot,
)
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.session_compat import (
    is_bridge_linked,
    read_approved_artifact,
    read_pending_bridge,
    read_program_id,
    read_program_phase,
    read_source_workflow_id,
    write_approved_artifact,
    write_pending_bridge,
    write_program_session,
    write_session_mode,
    write_source_workflow_id,
    write_workflow_phase,
)


def _requires_source_artifact(promotion: dict[str, Any]) -> bool:
    return bool(
        promotion.get("requires_source_artifact")
        or promotion.get("requires_approved_prd")
    )


def handle(ctx: HookContext) -> dict[str, Any]:
    session: SessionRecord = ctx.extras["session"]
    metadata = dict(session.metadata or {})
    bridge = dict(getattr(ctx.skeleton, "bridge", None) or {})
    summary = str(ctx.extras.get("summary") or "")
    target_workflow_type = str(
        ctx.extras.get("target_workflow_type")
        or bridge.get("target_workflow_type")
        or resolve_bridge_target_from_text(summary, str(ctx.skeleton.workflow_type or "prd_request"))
        or "",
    )
    if not target_workflow_type:
        raise ValueError("bridge target_workflow_type required")

    source_workflow_id = str(
        ctx.extras.get("source_workflow_id")
        or read_source_workflow_id(metadata)
        or "",
    )
    if not source_workflow_id:
        raise ValueError("source workflow_id required")

    artifact_key = str(bridge.get("artifact_session_key") or "approved_artifact")
    artifact = ctx.extras.get("approved_artifact") or read_approved_artifact(metadata)
    if not isinstance(artifact, dict):
        raise ValueError(f"{artifact_key} required to bridge workflow")

    if is_bridge_linked(metadata) and metadata.get("workflow_type") == target_workflow_type:
        raise ValueError("session already linked to bridge target workflow")

    target_skeleton = load_skeleton(target_workflow_type)
    promotion = dict(target_skeleton.promotion or {})
    if _requires_source_artifact(promotion) and not artifact:
        raise ValueError(f"{target_workflow_type} requires a source artifact")

    binding_key = str(metadata.get("binding_key") or ctx.extras.get("binding_key") or "")
    pending = read_pending_bridge(metadata) or {}
    if not summary:
        summary = str(pending.get("summary") or "")

    program_id = read_program_id(metadata)
    if program_id:
        artifacts = {"approved_prd": artifact}
        track = planning_track_for_workflow_type(target_workflow_type)
        if track:
            artifacts["planning_track"] = track
        pending_identity = read_pending_bridge(metadata) or {}
        target_phase = str(
            ctx.extras.get("target_node")
            or pending_identity.get("target_node")
            or ""
        ).strip() or None
        service = get_delivery_program_service()
        if not target_phase:
            try:
                program_payload = service.client.get_program(program_id)
                program = program_payload.get("program") or {}
                snapshot = parse_program_snapshot(program.get("program_definition_snapshot"))
                if snapshot is not None:
                    target_phase = node_id_for_workflow_type(snapshot, target_workflow_type)
            except Exception:
                target_phase = None
        if not target_phase:
            target_phase = phase_for_workflow_type(target_workflow_type)
        source_node = str(
            pending_identity.get("source_node")
            or read_program_phase(metadata)
            or ""
        ).strip()
        if source_node and target_phase:
            try:
                program_payload = service.client.get_program(program_id)
                program = program_payload.get("program") or {}
                snapshot = parse_program_snapshot(program.get("program_definition_snapshot"))
                if snapshot is not None:
                    from ts_harness.workflows.program_materialize import find_edge

                    if find_edge(snapshot, source=source_node, target=target_phase) is None:
                        raise ValueError(
                            f"cannot bridge from {source_node} to {target_phase} "
                            "for pinned program snapshot"
                        )
            except ValueError:
                raise
            except Exception:
                pass
        advance = service.advance_program(
            program_id=program_id,
            target_phase=target_phase,
            target_workflow_type=target_workflow_type,
            summary=summary,
            artifacts=artifacts,
            tenant_id=ctx.tenant_id,
            binding_key=binding_key or None,
            requester_actor_id=ctx.actor,
            source_workflow_id=source_workflow_id,
            actor=ctx.actor,
            decision=dict(ctx.extras.get("decision") or {}) or None,
        )
        try:
            ctx.memory_client.complete_workflow(
                source_workflow_id,
                actor=ctx.actor,
                reason=f"Phase complete; advanced to {advance.get('target_phase')}",
            )
        except Exception:
            pass

        target_workflow_id = str(advance["workflow_id"])
        target_workflow_type = str(advance["workflow_type"])
        task_ids = list(advance.get("task_ids") or [])
        intake = str(advance.get("first_step") or target_skeleton.intake_stage_name())

        updated = write_session_mode(metadata, "bridge_linked")
        updated.update(
            {
                "workflow_id": target_workflow_id,
                "workflow_type": target_workflow_type,
            },
        )
        updated = write_program_session(
            updated,
            program_id=program_id,
            program_phase=str(advance.get("target_phase") or ""),
            workflow_id=target_workflow_id,
            artifacts=artifacts,
        )
        updated = write_workflow_phase(updated, "approved")
        updated = write_source_workflow_id(updated, source_workflow_id)
        updated = write_approved_artifact(updated, artifact)
        updated = write_pending_bridge(updated, None)
        session.metadata = updated
        get_session_store().save(session)

        return {
            "status": "bridge_started",
            "target_workflow_id": target_workflow_id,
            "target_workflow_type": target_workflow_type,
            "source_workflow_id": source_workflow_id,
            "program_id": program_id,
            "task_ids": task_ids,
            "first_step": intake,
        }

    target_workflow_id = str(uuid.uuid4())
    ctx.memory_client.create_workflow(
        workflow_id=target_workflow_id,
        workflow_type=target_workflow_type,
        tenant_id=ctx.tenant_id,
        definition_version=target_skeleton.definition_version,
        source_channel=binding_key or None,
        parent_workflow_id=source_workflow_id,
        requester_actor_id=ctx.actor,
        actor=session.agent_id,
    )

    intake = target_skeleton.intake_stage_name()
    artifact_input_key = str(bridge.get("artifact_input_key") or artifact_key)
    stage_inputs = {
        intake: {
            artifact_input_key: artifact,
            "summary": summary,
            "source_workflow_id": source_workflow_id,
        },
    }
    if target_workflow_type == "work_orders_from_prd":
        stage_inputs[intake]["stage"] = "refinery"

    task_bodies = materialize_skeleton_tasks(
        target_skeleton,
        workflow_id=target_workflow_id,
        tenant_id=ctx.tenant_id,
        external_ref=binding_key or None,
        stage_inputs=stage_inputs,
    )

    name_to_id: dict[str, str] = {}
    task_ids: list[str] = []
    for body in task_bodies:
        depends_on_names = list(body.get("depends_on") or [])
        payload = {
            "workflow_id": target_workflow_id,
            "workflow_type": target_workflow_type,
            "tenant_id": ctx.tenant_id,
            "step_name": body["step_name"],
            "agent_id": body["agent_id"],
            "input": body.get("input") or {},
            "approval_required": body.get("approval_required", False),
            "max_retries": body.get("max_retries", 0),
            "external_ref": body.get("external_ref"),
            "contract": body.get("contract") or {},
            "definition_version": body.get("definition_version"),
            "lane": body.get("lane"),
            "actor": ctx.actor,
        }
        if depends_on_names:
            payload["depends_on"] = [name_to_id[name] for name in depends_on_names if name in name_to_id]
        result = ctx.memory_client.create_task(payload)
        task_id = str(result["id"])
        name_to_id[body["step_name"]] = task_id
        task_ids.append(task_id)

    try:
        ctx.memory_client.complete_workflow(
            source_workflow_id,
            actor=ctx.actor,
            reason=f"Source workflow bridged to {target_workflow_type}",
        )
    except Exception:
        pass

    updated = write_session_mode(metadata, "bridge_linked")
    updated.update(
        {
            "workflow_id": target_workflow_id,
            "workflow_type": target_workflow_type,
        },
    )
    updated = write_workflow_phase(updated, "approved")
    updated = write_source_workflow_id(updated, source_workflow_id)
    updated = write_approved_artifact(updated, artifact)
    updated = write_pending_bridge(updated, None)
    session.metadata = updated
    get_session_store().save(session)

    return {
        "status": "bridge_started",
        "target_workflow_id": target_workflow_id,
        "target_workflow_type": target_workflow_type,
        "source_workflow_id": source_workflow_id,
        "task_ids": task_ids,
        "first_step": intake,
    }
