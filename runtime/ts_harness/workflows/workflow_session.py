"""Generic workflow session phase tracking driven by skeleton config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.workflows.session_compat import (
    is_workflow_linked,
    read_source_workflow_id,
    read_workflow_phase,
    write_workflow_phase,
)


@dataclass(frozen=True)
class PhaseTrackingConfig:
    intake_step: str
    draft_steps: tuple[str, ...]
    gate_steps: tuple[str, ...]
    approved_value: str = "approved"
    drafting_value: str = "drafting"
    awaiting_approval_value: str = "awaiting_approval"


def get_phase_tracking(skeleton: Any) -> PhaseTrackingConfig | None:
    session_block = dict(getattr(skeleton, "session", None) or {})
    raw = session_block.get("phase_tracking")
    if not isinstance(raw, dict):
        return None
    intake_step = str(raw.get("intake_step") or skeleton.intake_stage_name())
    draft_steps = tuple(str(step) for step in raw.get("draft_steps") or [])
    gate_steps = tuple(str(step) for step in raw.get("gate_steps") or [])
    if not gate_steps and not draft_steps:
        return None
    return PhaseTrackingConfig(
        intake_step=intake_step,
        draft_steps=draft_steps,
        gate_steps=gate_steps,
        approved_value=str(raw.get("approved_value") or "approved"),
        drafting_value=str(raw.get("drafting_value") or "drafting"),
        awaiting_approval_value=str(raw.get("awaiting_approval_value") or "awaiting_approval"),
    )


def has_session_tracking(skeleton: Any) -> bool:
    return get_phase_tracking(skeleton) is not None


def derive_workflow_phase(tasks: list[dict[str, Any]], tracking: PhaseTrackingConfig) -> str:
    pending_gates = [
        task
        for task in tasks
        if str(task.get("step_name") or "") in tracking.gate_steps
        and task.get("approval_required")
        and task.get("approval_status") == "pending"
    ]
    if pending_gates:
        return tracking.awaiting_approval_value
    if any(
        str(task.get("step_name") or "") in tracking.gate_steps
        and task.get("approval_status") == "approved"
        for task in tasks
    ):
        return tracking.approved_value
    if any(str(task.get("step_name") or "") in tracking.draft_steps and task.get("status") == "succeeded" for task in tasks):
        return tracking.awaiting_approval_value
    if any(str(task.get("step_name") or "") == tracking.intake_step and task.get("status") == "succeeded" for task in tasks):
        return tracking.drafting_value
    if tasks:
        return tracking.drafting_value
    return "unknown"


def phase_for_step_transition(step_name: str, tracking: PhaseTrackingConfig) -> str | None:
    if step_name in tracking.draft_steps:
        return tracking.awaiting_approval_value
    if step_name == tracking.intake_step:
        return tracking.drafting_value
    return None


def sync_workflow_session_phase(
    session: SessionRecord,
    skeleton: Any,
    *,
    tenant_id: str | None = None,
) -> str | None:
    metadata = dict(session.metadata or {})
    if not is_workflow_linked(metadata):
        return read_workflow_phase(metadata)

    current = read_workflow_phase(metadata)
    tracking = get_phase_tracking(skeleton)
    if tracking is None:
        return current

    approved = tracking.approved_value
    if current == approved:
        return approved

    workflow_id = read_source_workflow_id(metadata) or str(metadata.get("workflow_id") or "")
    if not workflow_id:
        return current

    from ts_harness.workflows.engine import get_workflow_engine

    phase = get_workflow_engine().get_workflow_phase(
        workflow_id,
        skeleton.workflow_type,
        tenant_id=tenant_id or session.tenant_id,
    )
    if phase in {"unknown", ""}:
        return current

    if phase != current:
        metadata = write_workflow_phase(metadata, phase)
        session.metadata = metadata
        get_session_store().save(session)
    return phase


def set_workflow_phase_for_workflow(
    workflow_id: str,
    phase: str,
    skeleton: Any,
    *,
    tenant_id: str = "default",
) -> None:
    tracking = get_phase_tracking(skeleton)
    if tracking is None:
        return
    approved = tracking.approved_value
    for session in get_session_store().list_for_tenant(tenant_id):
        metadata = session.metadata or {}
        linked = metadata.get("workflow_id") == workflow_id or read_source_workflow_id(metadata) == workflow_id
        if not linked:
            continue
        if str(metadata.get("workflow_type") or "") not in {skeleton.workflow_type, ""} and not is_workflow_linked(metadata):
            continue
        if read_workflow_phase(metadata) == approved:
            continue
        updated = write_workflow_phase(dict(metadata), phase)
        session.metadata = updated
        get_session_store().save(session)


def sync_workflow_session_for_workflow(
    workflow_id: str,
    skeleton: Any,
    *,
    tenant_id: str = "default",
) -> None:
    if not has_session_tracking(skeleton):
        return
    for session in get_session_store().list_for_tenant(tenant_id):
        metadata = session.metadata or {}
        linked = metadata.get("workflow_id") == workflow_id or read_source_workflow_id(metadata) == workflow_id
        if linked and str(metadata.get("workflow_type") or "") in {skeleton.workflow_type, ""}:
            sync_workflow_session_phase(session, skeleton, tenant_id=tenant_id)
