"""Register delivery program membership on workflow promotion."""

from __future__ import annotations

from typing import Any
import uuid

from ts_harness.agent.sessions import SessionRecord
from ts_harness.workflows.program_materialize import program_pin_fields
from ts_harness.workflows.session_compat import write_program_session


def register_on_promote(
    *,
    session: SessionRecord,
    skeleton,
    workflow_id: str,
    summary: str,
    binding_key: str,
    tenant_id: str,
    actor: str,
    memory_client,
    artifacts: dict[str, Any] | None = None,
    program_id: str | None = None,
    program_type: str = "software_delivery",
) -> dict[str, Any] | None:
    program_cfg = dict(getattr(skeleton, "program", None) or {})
    phase = str(program_cfg.get("phase") or "").strip()
    if not phase:
        return None

    metadata = dict(session.metadata or {})
    resolved_program_id = str(program_id or metadata.get("program_id") or "")
    if not resolved_program_id:
        resolved_program_id = str(uuid.uuid4())
        pin_fields: dict[str, Any] = {}
        try:
            pin_fields = program_pin_fields(program_type)
        except Exception:
            # Legacy promote path: create without pins when registry unavailable.
            pin_fields = {}
        memory_client.create_program(
            program_id=resolved_program_id,
            tenant_id=tenant_id,
            summary=summary,
            entry_phase=phase,
            current_phase=phase,
            binding_key=binding_key or None,
            source_channel=binding_key or None,
            requester_actor_id=actor,
            artifacts=artifacts,
            actor=actor,
            **pin_fields,
        )

    memory_client.link_workflow_to_program(
        resolved_program_id,
        workflow_id=workflow_id,
        program_phase=phase,
        actor=actor,
    )

    session.metadata = write_program_session(
        metadata,
        program_id=resolved_program_id,
        program_phase=phase,
        workflow_id=workflow_id,
        artifacts=artifacts,
    )

    return {
        "status": "registered",
        "program_id": resolved_program_id,
        "program_phase": phase,
        "workflow_id": workflow_id,
    }


def handle(ctx) -> dict[str, Any]:
    session = ctx.extras.get("session")
    if session is None:
        return {"status": "skipped", "reason": "session required"}
    workflow_id = str(ctx.extras.get("workflow_id") or ctx.workflow_id or "")
    if not workflow_id:
        return {"status": "skipped", "reason": "workflow_id required"}
    result = register_on_promote(
        session=session,
        skeleton=ctx.skeleton,
        workflow_id=workflow_id,
        summary=str(ctx.extras.get("summary") or ""),
        binding_key=str(ctx.extras.get("binding_key") or ""),
        tenant_id=ctx.tenant_id,
        actor=ctx.actor or session.agent_id,
        memory_client=ctx.memory_client,
        artifacts=ctx.extras.get("artifacts") if isinstance(ctx.extras.get("artifacts"), dict) else None,
    )
    if result is None:
        return {"status": "skipped"}
    from ts_harness.agent.sessions import get_session_store

    get_session_store().save(session)
    return result
