"""Resolve two-phase delegation confirmation for channel ingress."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.gateway.confirmation_classifier import classify_confirmation_deterministic


@dataclass
class DelegateConfirmation:
    confirmed: bool = False
    cancelled: bool = False
    clear_pending: bool = False
    denied: str = ""


def resolve_delegate_confirmation(session: SessionRecord, user_text: str) -> DelegateConfirmation:
    pending = (session.metadata or {}).get("pending_delegate")
    if not pending:
        return DelegateConfirmation()
    classification = classify_confirmation_deterministic(user_text, context="delegate_start")
    if classification.decision == "reject":
        return DelegateConfirmation(cancelled=True, clear_pending=True)
    if classification.decision == "approve":
        from ts_harness.workflows.decisions import refusal_for_permission, resolve_decision_actor

        actor = resolve_decision_actor(session.metadata, fallback_actor="")
        denied = refusal_for_permission(actor, "confirm_delegation")
        if denied:
            return DelegateConfirmation(denied=denied)
        return DelegateConfirmation(confirmed=True)
    return DelegateConfirmation()


def clear_pending_delegate(session: SessionRecord) -> None:
    metadata = dict(session.metadata or {})
    metadata.pop("pending_delegate", None)
    session.metadata = metadata
    get_session_store().save(session)


def set_pending_delegate(
    session: SessionRecord,
    *,
    agent_id: str,
    task: str,
    input_data: dict | None = None,
) -> None:
    metadata = dict(session.metadata or {})
    metadata["pending_delegate"] = {
        "agent_id": agent_id,
        "task": task,
        "input": input_data or {},
    }
    session.metadata = metadata
    get_session_store().save(session)


@dataclass
class WorkflowConfirmation:
    confirmed: bool = False
    cancelled: bool = False
    clear_pending: bool = False
    denied: str = ""


def _confirmation_denial(
    session: SessionRecord,
    *,
    channel: str,
    auth: Any,
    actor_email: str | None,
    config: Any,
    permission: str,
) -> str:
    if auth is None:
        return ""
    from ts_harness.workflows.decisions import channel_permission_refusal

    return (
        channel_permission_refusal(
            session.metadata,
            channel=channel,
            auth=auth,
            permission=permission,
            actor_email=actor_email,
            config=config,
        )
        or ""
    )


def resolve_workflow_confirmation(
    session: SessionRecord,
    user_text: str,
    *,
    channel: str = "",
    auth: Any = None,
    actor_email: str | None = None,
    config: Any = None,
) -> WorkflowConfirmation:
    pending = (session.metadata or {}).get("pending_workflow")
    if not pending:
        return WorkflowConfirmation()
    classification = classify_confirmation_deterministic(user_text, context="workflow_start")
    if classification.decision == "reject":
        return WorkflowConfirmation(cancelled=True, clear_pending=True)
    if classification.decision == "approve":
        denied = _confirmation_denial(
            session, channel=channel, auth=auth, actor_email=actor_email, config=config, permission="start_program"
        )
        if denied:
            return WorkflowConfirmation(denied=denied)
        return WorkflowConfirmation(confirmed=True)
    return WorkflowConfirmation()


def clear_pending_workflow(session: SessionRecord) -> None:
    metadata = dict(session.metadata or {})
    metadata.pop("pending_workflow", None)
    session.metadata = metadata
    get_session_store().save(session)


def set_pending_workflow(
    session: SessionRecord,
    *,
    workflow_type: str,
    summary: str,
    input_data: dict | None = None,
    program_type: str | None = None,
) -> None:
    metadata = dict(session.metadata or {})
    pending = {
        "workflow_type": workflow_type,
        "summary": summary,
        "input": input_data or {},
    }
    if program_type:
        pending["program_type"] = program_type
    metadata["pending_workflow"] = pending
    session.metadata = metadata
    get_session_store().save(session)


def apply_workflow_confirmation_to_session(
    session: SessionRecord, confirmation: WorkflowConfirmation
) -> None:
    if confirmation.clear_pending:
        clear_pending_workflow(session)


def apply_confirmation_to_session(session: SessionRecord, confirmation: DelegateConfirmation) -> None:
    if confirmation.clear_pending:
        clear_pending_delegate(session)


@dataclass
class ContinueDeliveryConfirmation:
    confirmed: bool = False
    cancelled: bool = False
    clear_pending: bool = False
    denied: str = ""


BridgeConfirmation = ContinueDeliveryConfirmation


def resolve_continue_delivery_confirmation(
    session: SessionRecord,
    user_text: str,
    *,
    channel: str = "",
    auth: Any = None,
    actor_email: str | None = None,
    config: Any = None,
) -> ContinueDeliveryConfirmation:
    return resolve_bridge_confirmation(
        session, user_text, channel=channel, auth=auth, actor_email=actor_email, config=config
    )


def resolve_bridge_confirmation(
    session: SessionRecord,
    user_text: str,
    *,
    channel: str = "",
    auth: Any = None,
    actor_email: str | None = None,
    config: Any = None,
) -> BridgeConfirmation:
    metadata = session.metadata or {}
    pending = metadata.get("pending_bridge") or metadata.get("pending_continue_delivery")
    if not pending:
        return BridgeConfirmation()
    classification = classify_confirmation_deterministic(user_text, context="bridge_start")
    if classification.decision == "reject":
        return BridgeConfirmation(cancelled=True, clear_pending=True)
    if classification.decision == "approve":
        denied = _confirmation_denial(
            session, channel=channel, auth=auth, actor_email=actor_email, config=config, permission="start_program"
        )
        if denied:
            return BridgeConfirmation(denied=denied)
        return BridgeConfirmation(confirmed=True)
    return BridgeConfirmation()


def clear_pending_continue_delivery(session: SessionRecord) -> None:
    clear_pending_bridge(session)


def clear_pending_bridge(session: SessionRecord) -> None:
    from ts_harness.workflows.session_compat import write_pending_bridge

    metadata = write_pending_bridge(dict(session.metadata or {}), None)
    session.metadata = metadata
    get_session_store().save(session)


def set_pending_continue_delivery(
    session: SessionRecord,
    *,
    source_workflow_id: str,
    summary: str,
) -> None:
    set_pending_bridge(session, source_workflow_id=source_workflow_id, summary=summary)


def set_pending_bridge(
    session: SessionRecord,
    *,
    source_workflow_id: str,
    summary: str,
    target_workflow_type: str | None = None,
    program_type: str | None = None,
    source_node: str | None = None,
    target_node: str | None = None,
) -> None:
    from ts_harness.workflows.session_compat import write_pending_bridge

    pending = {
        "source_workflow_id": source_workflow_id,
        "summary": summary,
    }
    if target_workflow_type:
        pending["target_workflow_type"] = target_workflow_type
    if program_type:
        pending["program_type"] = program_type
    if source_node:
        pending["source_node"] = source_node
    if target_node:
        pending["target_node"] = target_node
    metadata = write_pending_bridge(dict(session.metadata or {}), pending)
    session.metadata = metadata
    get_session_store().save(session)


def apply_continue_delivery_confirmation_to_session(
    session: SessionRecord, confirmation: ContinueDeliveryConfirmation
) -> None:
    apply_bridge_confirmation_to_session(session, confirmation)


def apply_bridge_confirmation_to_session(
    session: SessionRecord, confirmation: BridgeConfirmation
) -> None:
    if confirmation.clear_pending:
        clear_pending_bridge(session)


@dataclass
class ProgramConfirmation:
    confirmed: bool = False
    cancelled: bool = False
    clear_pending: bool = False
    denied: str = ""


def resolve_program_confirmation(
    session: SessionRecord,
    user_text: str,
    *,
    channel: str = "",
    auth: Any = None,
    actor_email: str | None = None,
    config: Any = None,
) -> ProgramConfirmation:
    pending = (session.metadata or {}).get("pending_program")
    if not pending:
        return ProgramConfirmation()
    classification = classify_confirmation_deterministic(user_text, context="workflow_start")
    if classification.decision == "reject":
        return ProgramConfirmation(cancelled=True, clear_pending=True)
    if classification.decision == "approve":
        from ts_harness.gateway.program_definitions_bff import program_action_permission

        denied = _confirmation_denial(
            session,
            channel=channel,
            auth=auth,
            actor_email=actor_email,
            config=config,
            permission=program_action_permission(
                str(pending.get("program_type") or ""),
                str(pending.get("entry_phase") or "") or None,
            ),
        )
        if denied:
            return ProgramConfirmation(denied=denied)
        return ProgramConfirmation(confirmed=True)
    return ProgramConfirmation()


def clear_pending_program(session: SessionRecord) -> None:
    from ts_harness.workflows.session_compat import write_pending_program

    metadata = write_pending_program(dict(session.metadata or {}), None)
    session.metadata = metadata
    get_session_store().save(session)


def set_pending_program(
    session: SessionRecord,
    *,
    entry_phase: str,
    summary: str,
    artifacts: dict | None = None,
    program_type: str | None = None,
    project_id: str | None = None,
    project_id_source: str | None = None,
    link_program_ids: list | None = None,
) -> None:
    from ts_harness.workflows.session_compat import write_pending_program

    pending = {
        "entry_phase": entry_phase,
        "summary": summary,
        "artifacts": artifacts or {},
        "program_type": str(program_type or "software_delivery"),
    }
    if project_id:
        pending["project_id"] = project_id
    if project_id_source:
        pending["project_id_source"] = project_id_source
    if link_program_ids:
        pending["link_program_ids"] = list(link_program_ids)
    metadata = write_pending_program(dict(session.metadata or {}), pending)
    session.metadata = metadata
    get_session_store().save(session)


def apply_program_confirmation_to_session(
    session: SessionRecord, confirmation: ProgramConfirmation
) -> None:
    if confirmation.clear_pending:
        clear_pending_program(session)


@dataclass
class ProgramAdvanceConfirmation:
    confirmed: bool = False
    cancelled: bool = False
    clear_pending: bool = False
    denied: str = ""


def resolve_program_advance_confirmation(
    session: SessionRecord,
    user_text: str,
    *,
    channel: str = "",
    auth: Any = None,
    actor_email: str | None = None,
    config: Any = None,
) -> ProgramAdvanceConfirmation:
    pending = (session.metadata or {}).get("pending_program_advance")
    if not pending:
        return ProgramAdvanceConfirmation()
    classification = classify_confirmation_deterministic(user_text, context="bridge_start")
    if classification.decision == "reject":
        return ProgramAdvanceConfirmation(cancelled=True, clear_pending=True)
    if classification.decision == "approve":
        from ts_harness.gateway.program_definitions_bff import program_action_permission

        target = str(pending.get("target_node") or pending.get("target_phase") or "") or None
        denied = _confirmation_denial(
            session,
            channel=channel,
            auth=auth,
            actor_email=actor_email,
            config=config,
            permission=program_action_permission(str(pending.get("program_type") or ""), target),
        )
        if denied:
            return ProgramAdvanceConfirmation(denied=denied)
        return ProgramAdvanceConfirmation(confirmed=True)
    return ProgramAdvanceConfirmation()


def clear_pending_program_advance(session: SessionRecord) -> None:
    from ts_harness.workflows.session_compat import write_pending_program_advance

    metadata = write_pending_program_advance(dict(session.metadata or {}), None)
    session.metadata = metadata
    get_session_store().save(session)


def set_pending_program_advance(
    session: SessionRecord,
    *,
    program_id: str,
    summary: str,
    target_phase: str | None = None,
    program_type: str | None = None,
    source_node: str | None = None,
    target_node: str | None = None,
) -> None:
    from ts_harness.workflows.session_compat import write_pending_program_advance

    pending = {
        "program_id": program_id,
        "summary": summary,
    }
    if target_phase:
        pending["target_phase"] = target_phase
    if program_type:
        pending["program_type"] = program_type
    if source_node:
        pending["source_node"] = source_node
    if target_node:
        pending["target_node"] = target_node
        pending.setdefault("target_phase", target_node)
    metadata = write_pending_program_advance(dict(session.metadata or {}), pending)
    session.metadata = metadata
    get_session_store().save(session)


def apply_program_advance_confirmation_to_session(
    session: SessionRecord, confirmation: ProgramAdvanceConfirmation
) -> None:
    if confirmation.clear_pending:
        clear_pending_program_advance(session)
