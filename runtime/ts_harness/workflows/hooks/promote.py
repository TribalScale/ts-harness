"""Promote channel session to durable workflow."""

from __future__ import annotations

from typing import Any
import uuid

from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.gateway.channels.slack_users import (
    channel_requester_actor_id,
    slack_user_id_from_actor,
)
from ts_harness.workflows.hooks.register_program import register_on_promote
from ts_harness.workflows.materialize import materialize_skeleton_tasks
from ts_harness.workflows.registry import HookContext

MAX_TRANSCRIPT_TURNS = 20


def handle(ctx: HookContext) -> dict[str, Any]:
    session: SessionRecord = ctx.extras["session"]
    summary = str(ctx.extras.get("summary") or "")
    binding_key = str(ctx.extras.get("binding_key") or "")
    transcript_messages = list(ctx.extras.get("transcript_messages") or [])
    extra_input = (
        ctx.extras.get("extra_input")
        if isinstance(ctx.extras.get("extra_input"), dict)
        else {}
    )

    if (session.metadata or {}).get("workflow_id"):
        raise ValueError("session already linked to a workflow")

    session_metadata = dict(session.metadata or {})
    requester_actor_id = channel_requester_actor_id(session_metadata, ctx.actor)

    workflow_id = str(uuid.uuid4())
    program_cfg = dict(getattr(ctx.skeleton, "program", None) or {})
    program_phase = str(program_cfg.get("phase") or "").strip() or None
    program_id = str((session.metadata or {}).get("program_id") or "")
    if program_phase and not program_id:
        program_id = str(uuid.uuid4())

    existing_program_id = (session.metadata or {}).get("program_id")
    pin_fields: dict[str, Any] = {}
    if program_phase and program_id and not existing_program_id:
        try:
            from ts_harness.workflows.program_materialize import program_pin_fields

            pin_fields = program_pin_fields("software_delivery")
        except Exception:
            pin_fields = {}
        ctx.memory_client.create_program(
            program_id=program_id,
            tenant_id=ctx.tenant_id,
            summary=summary,
            entry_phase=program_phase,
            current_phase=program_phase,
            binding_key=binding_key or None,
            source_channel=binding_key or None,
            requester_actor_id=requester_actor_id,
            artifacts=extra_input if extra_input else None,
            actor=session.agent_id,
            **pin_fields,
        )

    definition_version = ctx.skeleton.definition_version
    if pin_fields:
        resolved = pin_fields.get("resolved_workflow_versions") or {}
        node_pin = resolved.get(program_phase or "") if isinstance(resolved, dict) else None
        if isinstance(node_pin, dict):
            pins = node_pin.get("pins") if isinstance(node_pin.get("pins"), dict) else {}
            pinned = pins.get(ctx.skeleton.workflow_type)
            if pinned:
                definition_version = str(pinned)

    ctx.memory_client.create_workflow(
        workflow_id=workflow_id,
        workflow_type=ctx.skeleton.workflow_type,
        tenant_id=ctx.tenant_id,
        definition_version=definition_version,
        source_channel=binding_key,
        requester_actor_id=requester_actor_id,
        correlation_id=(
            f"program:{program_id}" if program_id else ctx.extras.get("correlation_id")
        ),
        program_id=program_id or None,
        program_phase=program_phase,
        actor=session.agent_id,
    )

    context = _build_context(summary, transcript_messages, extra_input)
    intake_stage = ctx.skeleton.intake_stage_name()
    stage_inputs = {intake_stage: {"context": context, "summary": summary}}

    task_bodies = materialize_skeleton_tasks(
        ctx.skeleton,
        workflow_id=workflow_id,
        tenant_id=ctx.tenant_id,
        external_ref=binding_key,
        stage_inputs=stage_inputs,
    )

    name_to_id: dict[str, str] = {}
    task_ids: list[str] = []
    for body in task_bodies:
        depends_on_names = list(body.get("depends_on") or [])
        payload = {
            "workflow_id": workflow_id,
            "workflow_type": ctx.skeleton.workflow_type,
            "tenant_id": ctx.tenant_id,
            "step_name": body["step_name"],
            "agent_id": body["agent_id"],
            "input": body.get("input") or {},
            "approval_required": body.get("approval_required", False),
            "max_retries": body.get("max_retries", 0),
            "external_ref": body.get("external_ref"),
            "contract": body.get("contract") or {},
            "definition_version": body.get("definition_version"),
            "actor": session.agent_id,
        }
        if depends_on_names:
            payload["depends_on"] = [
                name_to_id[name] for name in depends_on_names if name in name_to_id
            ]
        result = ctx.memory_client.create_task(payload)
        task_id = str(result["id"])
        name_to_id[body["step_name"]] = task_id
        task_ids.append(task_id)

    metadata = dict(session_metadata)
    slack_user_id = slack_user_id_from_actor(requester_actor_id)
    metadata.update(
        {
            "workflow_id": workflow_id,
            "workflow_type": ctx.skeleton.workflow_type,
            "binding_key": binding_key,
            "mode": "workflow_linked",
            "pending_workflow": None,
            "requester_actor_id": requester_actor_id,
        },
    )
    if slack_user_id:
        metadata["slack_user_id"] = slack_user_id
    from ts_harness.workflows.workflow_session import get_phase_tracking
    from ts_harness.workflows.session_compat import write_workflow_phase

    tracking = get_phase_tracking(ctx.skeleton)
    if tracking is not None:
        metadata = write_workflow_phase(metadata, tracking.drafting_value)

    session.metadata = metadata
    register_result = register_on_promote(
        session=session,
        skeleton=ctx.skeleton,
        workflow_id=workflow_id,
        summary=summary,
        binding_key=binding_key,
        tenant_id=ctx.tenant_id,
        actor=ctx.actor,
        memory_client=ctx.memory_client,
        artifacts=extra_input if extra_input else None,
        program_id=program_id or None,
    )
    if register_result is not None:
        metadata = dict(session.metadata or metadata)

    session.metadata = metadata
    get_session_store().save(session)

    result = {
        "workflow_id": workflow_id,
        "workflow_type": ctx.skeleton.workflow_type,
        "task_ids": task_ids,
        "first_step": intake_stage,
        "binding_key": binding_key,
    }
    if register_result:
        result.update(register_result)
    return result


def _build_context(
    summary: str,
    transcript_messages: list[dict[str, Any]] | None,
    extra_input: dict[str, Any] | None,
) -> dict[str, Any]:
    turns = list(transcript_messages or [])[-MAX_TRANSCRIPT_TURNS:]
    return {
        "summary": summary,
        "transcript": turns,
        **(extra_input or {}),
    }
