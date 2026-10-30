"""Execute confirmed channel actions without relying on the LLM tool loop."""

from __future__ import annotations

import re
from typing import Any
import uuid

from ts_harness.agent.provider import ModelProvider
from ts_harness.agent.sessions import SessionRecord
from ts_harness.gateway.confirmation_classifier import (
    ConfirmationClassification,
    classify_confirmation,
    confirmation_clarification_message,
    looks_like_revision_feedback,
)
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.routing.intents import ChannelIntent
from ts_harness.tools.workflow_tools import (
    advance_program_handler,
    bridge_workflow_handler,
    propose_bridge_workflow_handler,
    propose_start_program_handler,
    propose_start_workflow_handler,
    start_program_handler,
    start_workflow_handler,
    workflow_status_handler,
)
from ts_harness.workflows.artifacts import (
    artifact_steps,
    format_artifact_for_channel,
    latest_artifact_output,
)

_STATUS_QUERY_RE = re.compile(
    r"(?:"
    r"what'?s?(?:\s+is)?\s+(?:the\s+)?(?:workflow\s+)?status|"
    r"(?:workflow|prd|request)\s+status|"
    r"(?:where|how)\s+(?:are|is)\s+we|"
    r"what'?s?(?:\s+is)?\s+(?:the\s+)?progress|"
    r"did\s+(?:my|the)\s+(?:request|workflow)\s+(?:finish|complete)"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_QUERY_RE = re.compile(
    r"(?:"
    r"where(?:'?s|\s+is)\s+(?:the\s+|my\s+)?(?:prd|draft|document|artifact|rfp)|"
    r"show(?:\s+me)?\s+(?:the\s+)?(?:prd|draft|document|artifact|rfp)|"
    r"(?:can\s+i\s+see|view|read)\s+(?:the\s+)?(?:prd|draft|document|artifact|rfp)|"
    r"send(?:\s+me)?\s+(?:the\s+)?(?:prd|draft|document|artifact|rfp)"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_PROVENANCE_RE = re.compile(
    r"(?:"
    r"where did (?:this|the draft|the prd) come from|"
    r"is this from|did this come from|"
    r"is (?:this|the draft|the prd) from|"
    r"which (?:step|task|agent).*(?:this|draft|prd)|"
    r"who (?:wrote|created|drafted) (?:this|the (?:draft|prd))"
    r")",
    re.IGNORECASE,
)
_CONFIRM_ONLY_RE = re.compile(
    r"^(?:yes|y|yeah|yep|no|n|nope|cancel|approve|approved|confirm|confirmed|proceed|ok|okay|sure)$",
    re.IGNORECASE,
)
_SKIP_AUTO_PROPOSE_INTENTS = frozenset(
    {"workflow_gate", "continue_bridge", "direct_respond", "ad_hoc_delegate"}
)


def _channel_policy_context(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    workflow_confirmed: bool = False,
    bridge_confirmed: bool = False,
    program_confirmed: bool = False,
    program_advance_confirmed: bool = False,
    actor_email: str | None = None,
) -> ToolPolicyContext:
    metadata = session.metadata or {}
    return ToolPolicyContext(
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
        channel_ingress=True,
        session_id=session.session_id,
        workflow_confirmed=workflow_confirmed,
        pending_workflow=metadata.get("pending_workflow"),
        workflow_linked=bool(metadata.get("workflow_id")),
        linked_workflow_id=str(metadata.get("workflow_id") or "") or None,
        bridge_confirmed=bridge_confirmed,
        continue_delivery_confirmed=bridge_confirmed,
        pending_bridge=metadata.get("pending_bridge") or metadata.get("pending_continue_delivery"),
        pending_continue_delivery=metadata.get("pending_bridge") or metadata.get("pending_continue_delivery"),
        program_confirmed=program_confirmed,
        pending_program=metadata.get("pending_program"),
        program_advance_confirmed=program_advance_confirmed,
        pending_program_advance=metadata.get("pending_program_advance"),
        workflow_phase=str(metadata.get("workflow_phase") or metadata.get("prd_phase") or "") or None,
        session_mode=str(metadata.get("mode") or "") or None,
        actor_email=actor_email,
    )


def should_auto_propose_workflow(intent: ChannelIntent, metadata: dict) -> bool:
    if intent.kind in _SKIP_AUTO_PROPOSE_INTENTS:
        return False
    if intent.program_type:
        return False
    if not intent.workflow_type:
        return False
    if metadata.get("workflow_id"):
        return False
    return True


def should_auto_propose_program(intent: ChannelIntent, metadata: dict) -> bool:
    if intent.kind in _SKIP_AUTO_PROPOSE_INTENTS:
        return False
    if not intent.program_type:
        return False
    if metadata.get("workflow_id") or metadata.get("program_id"):
        return False
    return True


def execute_governed_workflow_proposal(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    workflow_type: str,
    summary: str,
    input_data: dict | None = None,
) -> dict[str, Any]:
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
    )
    return propose_start_workflow_handler(
        {
            "workflow_type": workflow_type,
            "summary": summary,
            "input": input_data or {},
        },
        ctx,
    )


def execute_governed_program_proposal(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    program_type: str,
    summary: str,
    entry_phase: str | None = None,
    artifacts: dict | None = None,
) -> dict[str, Any]:
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
    )
    args: dict[str, Any] = {
        "program_type": program_type,
        "summary": summary,
        "artifacts": artifacts or {},
    }
    if entry_phase:
        args["entry_phase"] = entry_phase
    return propose_start_program_handler(args, ctx)


def execute_confirmed_workflow_start(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
) -> dict[str, Any] | None:
    pending = (session.metadata or {}).get("pending_workflow")
    if not pending:
        return None
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
        workflow_confirmed=True,
    )
    return start_workflow_handler(
        {
            "workflow_type": pending.get("workflow_type"),
            "summary": pending.get("summary"),
            "input": pending.get("input") if isinstance(pending.get("input"), dict) else {},
        },
        ctx,
    )


def execute_confirmed_program_start(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    actor_email: str | None = None,
) -> dict[str, Any] | None:
    pending = (session.metadata or {}).get("pending_program")
    if not pending:
        return None
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
        program_confirmed=True,
        actor_email=actor_email,
    )
    artifacts = pending.get("artifacts") if isinstance(pending.get("artifacts"), dict) else {}
    return start_program_handler(
        {
            "entry_phase": pending.get("entry_phase"),
            "summary": pending.get("summary"),
            "artifacts": artifacts,
            "program_type": pending.get("program_type"),
        },
        ctx,
    )


def execute_confirmed_program_advance(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
) -> dict[str, Any] | None:
    pending = (session.metadata or {}).get("pending_program_advance")
    if not pending:
        return None
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
        program_advance_confirmed=True,
        bridge_confirmed=True,
    )
    payload: dict[str, Any] = {
        "program_id": pending.get("program_id"),
        "summary": pending.get("summary"),
    }
    if pending.get("target_phase"):
        payload["target_phase"] = pending.get("target_phase")
    if pending.get("target_node"):
        payload["target_node"] = pending.get("target_node")
    if pending.get("program_type"):
        payload["program_type"] = pending.get("program_type")
    return advance_program_handler(payload, ctx)


def execute_confirmed_bridge(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
) -> dict[str, Any] | None:
    metadata = session.metadata or {}
    pending = metadata.get("pending_bridge") or metadata.get("pending_continue_delivery")
    if not pending:
        return None
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
        bridge_confirmed=True,
    )
    payload: dict[str, Any] = {
        "summary": pending.get("summary"),
        "target_workflow_type": pending.get("target_workflow_type"),
    }
    if pending.get("target_node"):
        payload["target_node"] = pending.get("target_node")
    if pending.get("program_type"):
        payload["program_type"] = pending.get("program_type")
    return bridge_workflow_handler(payload, ctx)


def execute_bridge_workflow_proposal(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    summary: str,
) -> dict[str, Any]:
    metadata = session.metadata or {}
    workflow_type = str(metadata.get("workflow_type") or "")
    target_workflow_type = ""
    if workflow_type:
        from ts_harness.workflows.delivery_program import resolve_bridge_target_from_text

        target_workflow_type = resolve_bridge_target_from_text(
            summary,
            workflow_type,
            program_type=str(metadata.get("program_type") or "software_delivery"),
            source_node=str(metadata.get("program_phase") or "") or None,
        ) or ""
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
    )
    return propose_bridge_workflow_handler(
        {
            "summary": summary,
            "target_workflow_type": target_workflow_type,
            "program_type": metadata.get("program_type"),
        },
        ctx,
    )


def format_bridge_proposal_message(result: dict[str, Any]) -> str:
    target = str(result.get("target_workflow_type") or "downstream workflow")
    summary = str(result.get("summary") or "").strip()
    label = target.replace("_", " ")
    if summary:
        lead = f"I've prepared the {label} continuation for: {summary}"
    else:
        lead = f"I've prepared the {label} continuation."
    return f"{lead}\nReply yes to start, or no to cancel."


def format_program_proposal_message(result: dict[str, Any]) -> str:
    program_type = str(result.get("program_type") or "program")
    entry_phase = str(result.get("entry_phase") or "").strip()
    summary = str(result.get("summary") or "").strip()
    lines = [f"I can start the `{program_type}` program."]
    if entry_phase:
        lines.append(f"Entry phase: `{entry_phase}`.")
    if summary:
        lines.append(f"Summary: {summary}")
    lines.append("Reply **yes** to start, or **no** to cancel.")
    return "\n".join(lines)


_PROPOSAL_SUMMARY_LIMIT = 280


def _present_proposal_summary(summary: str, *, limit: int = _PROPOSAL_SUMMARY_LIMIT) -> str:
    """Shorten a long request so the confirmation stays readable.

    The full text stays on the workflow input. This only changes the channel message.
    """
    text = " ".join(summary.split()).strip()
    if len(text) <= limit:
        return text
    for index, char in enumerate(text):
        if char not in ".!?" or index + 1 >= len(text) or not text[index + 1].isspace():
            continue
        sentence_len = index + 1
        if sentence_len > limit:
            break
        if sentence_len >= 40:
            return text[:sentence_len].strip()
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= 40:
        cut = cut[:space]
    return cut.rstrip(".,;:") + "…"


def format_workflow_proposal_message(result: dict[str, Any]) -> str:
    workflow_type = str(result.get("workflow_type") or "workflow").replace("_", " ")
    summary = _present_proposal_summary(str(result.get("summary") or ""))
    if summary:
        lead = f"I've prepared a {workflow_type} workflow for: {summary}"
    else:
        lead = f"I've prepared a {workflow_type} workflow."
    return f"{lead}\nReply yes to start, or no to cancel."


def format_workflow_started_message(result: dict[str, Any]) -> str:
    workflow_type = str(result.get("workflow_type") or "workflow").replace("_", " ")
    first_step = str(result.get("first_step") or "intake")
    return (
        f"Started the {workflow_type} workflow. "
        f"The first step ({first_step.replace('_', ' ')}) is queued and will run shortly."
    )


def format_auto_action_failure_message(*, action_label: str, error_reason: str) -> str:
    reason = str(error_reason or "unknown error").strip()
    return f"I could not {action_label}: {reason}. Please try again."


def format_program_started_message(result: dict[str, Any]) -> str:
    entry_phase = str(result.get("entry_phase") or "delivery").replace("_", " ")
    workflow_type = str(result.get("workflow_type") or "workflow").replace("_", " ")
    first_step = str(result.get("first_step") or "next step").replace("_", " ")
    return (
        f"Started the delivery program at the {entry_phase} phase ({workflow_type}). "
        f"The first step ({first_step}) is queued and will run shortly."
    )


def format_program_advanced_message(result: dict[str, Any]) -> str:
    target_phase = str(result.get("target_phase") or "").replace("_", " ")
    workflow_type = str(result.get("workflow_type") or "downstream workflow").replace("_", " ")
    first_step = str(result.get("first_step") or "next step").replace("_", " ")
    if target_phase:
        return (
            f"Advanced the delivery program to the {target_phase} phase ({workflow_type}). "
            f"The first step ({first_step}) is queued and will run shortly."
        )
    return format_bridge_started_message(result)


def _workflow_tasks(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    workflow_id = metadata.get("workflow_id")
    if not workflow_id:
        return []
    from ts_harness.workflows.engine import get_workflow_engine

    status = get_workflow_engine().get_status(str(workflow_id))
    return list(status.get("tasks") or [])


def should_auto_handle_intake_response(
    metadata: dict,
    user_text: str,
    *,
    tasks: list[dict[str, Any]] | None = None,
) -> bool:
    if not metadata.get("workflow_id"):
        return False
    text = (user_text or "").strip()
    if not text:
        return False
    if is_workflow_status_query(text) or is_workflow_artifact_query(text):
        return False
    from ts_harness.workflows.intake import task_awaiting_intake_response

    task_list = tasks if tasks is not None else _workflow_tasks(metadata)
    return task_awaiting_intake_response(task_list) is not None


def execute_intake_response(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    user_text: str,
) -> dict[str, Any]:
    from ts_harness.workflows.engine import get_workflow_engine
    from ts_harness.workflows.intake import (
        INTAKE_RESPONSE_EVENT,
        intake_original_request,
        intake_task_output,
        merge_intake_response,
        task_has_external_wait,
        task_awaiting_intake_response,
    )

    metadata = session.metadata or {}
    workflow_id = str(metadata.get("workflow_id") or "")
    if not workflow_id:
        return {"error": "session not linked to a workflow"}

    engine = get_workflow_engine()
    status = engine.get_status(workflow_id)
    tasks = list(status.get("tasks") or [])
    waiting = task_awaiting_intake_response(tasks)
    if waiting is None:
        return {"error": "workflow is not awaiting intake answers"}

    pending = metadata.get("pending_workflow") or {}
    summary = str(pending.get("summary") or user_text).strip()
    merged = merge_intake_response(
        intake_task_output(tasks),
        user_text,
        summary=summary,
        original_request=intake_original_request(tasks),
    )
    if not task_has_external_wait(waiting):
        task_id = str(waiting.get("id") or "")
        if not task_id:
            return {"error": "workflow is not awaiting intake answers"}
        engine.client.set_task_wait(
            task_id,
            wait_token=str(uuid.uuid4()),
            expected_event_type=INTAKE_RESPONSE_EVENT,
            actor=agent_id,
        )
        engine.client.block_task_api(
            task_id,
            f"awaiting_external:{INTAKE_RESPONSE_EVENT}",
            actor=agent_id,
        )
        result = engine.client.resume_external(
            workflow_id,
            event_type=INTAKE_RESPONSE_EVENT,
            payload=dict(merged["external_event"]),
            actor=agent_id,
            task_output=merged,
        )
        payload = engine.client.get_workflow(workflow_id)
        task = next(
            (item for item in (payload.get("tasks") or []) if str(item.get("id")) == task_id),
            None,
        )
        if task is not None:
            engine.on_task_transition(
                task,
                "succeeded",
                dispatch_result={"output": task.get("output") or merged},
            )
        return {
            "status": result.get("status") or "resumed",
            "workflow_id": workflow_id,
            "task_id": str(result.get("task_id") or task_id),
        }
    return engine.resume_external(
        workflow_id,
        event_type=INTAKE_RESPONSE_EVENT,
        payload=dict(merged["external_event"]),
        task_output=merged,
        actor=agent_id,
        tenant_id=tenant_id,
    )


def format_intake_response_ack(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"I couldn't record your intake answers: {result['error']}"
    return (
        "Thanks — I recorded your answers and queued the PRD draft step. "
        "I'll post the draft here when it's ready."
    )


def has_actionable_approval_gate(
    metadata: dict,
    *,
    tasks: list[dict[str, Any]] | None = None,
) -> bool:
    if not metadata.get("workflow_id"):
        return False
    task_list = tasks if tasks is not None else _workflow_tasks(metadata)
    return bool(_actionable_pending_approval_ids(task_list))


def classify_approval_gate_response(
    user_text: str,
    *,
    provider: ModelProvider | None = None,
    use_llm_fallback: bool = True,
) -> ConfirmationClassification:
    return classify_confirmation(
        user_text,
        context="approval_gate",
        provider=provider,
        use_llm_fallback=use_llm_fallback,
    )


def should_intercept_approval_gate_reply(
    metadata: dict,
    user_text: str,
    *,
    tasks: list[dict[str, Any]] | None = None,
) -> bool:
    if not has_actionable_approval_gate(metadata, tasks=tasks):
        return False
    text = (user_text or "").strip()
    if not text:
        return False
    if is_workflow_status_query(text) or is_workflow_artifact_query(text):
        return False
    return True


def should_auto_handle_workflow_gate_response(
    metadata: dict,
    user_text: str,
    *,
    tasks: list[dict[str, Any]] | None = None,
) -> bool:
    if not should_intercept_approval_gate_reply(metadata, user_text, tasks=tasks):
        return False
    classification = classify_approval_gate_response(
        user_text,
        provider=None,
        use_llm_fallback=False,
    )
    return classification.decision in {"approve", "reject"}


def _pending_gate_stage(session: SessionRecord) -> tuple[dict[str, Any] | None, Any]:
    try:
        tasks = _workflow_tasks(session.metadata or {})
    except Exception:  # noqa: BLE001
        return None, None
    actionable = _actionable_pending_approval_ids(tasks)
    gate = next((task for task in tasks if str(task.get("id") or "") in actionable), None)
    if gate is None:
        return None, None
    try:
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton(str(gate.get("workflow_type") or ""))
        return gate, skeleton.stage(str(gate.get("step_name") or ""))
    except Exception:  # noqa: BLE001
        return gate, None


def _gate_program_id(session: SessionRecord, gate: dict[str, Any]) -> str:
    task_input = gate.get("input") if isinstance(gate.get("input"), dict) else {}
    return str(task_input.get("program_id") or (session.metadata or {}).get("program_id") or "")


def gate_clarification_message(session: SessionRecord) -> str:
    from ts_harness.workflows.repo_pick import allowlisted_repos, is_repo_pick_stage, repo_pick_prompt

    gate, stage = _pending_gate_stage(session)
    if gate is None:
        return format_approval_gate_clarification()
    if is_repo_pick_stage(stage):
        return repo_pick_prompt(allowlisted_repos(_gate_program_id(session, gate)))
    if str(gate.get("step_name") or "") == "approve_prd":
        return format_approval_gate_clarification()
    return (
        f"Do you want to approve `{gate.get('step_name')}`, or request changes? "
        "Reply approve, or reply no with the changes you want."
    )


def _repo_pick_reply(session: SessionRecord, user_text: str) -> bool:
    """True when the pending gate is a repo pick and the reply names exactly one allowlisted repo."""
    from ts_harness.workflows.repo_pick import allowlisted_repos, is_repo_pick_stage, mentioned_repos

    gate, stage = _pending_gate_stage(session)
    if gate is None or not is_repo_pick_stage(stage):
        return False
    return len(mentioned_repos(user_text, allowlisted_repos(_gate_program_id(session, gate)))) == 1


def execute_workflow_gate_classification(
    session: SessionRecord,
    classification: ConfirmationClassification,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    reply_text: str = "",
) -> dict[str, Any]:
    del source
    metadata = session.metadata or {}
    workflow_id = str(metadata.get("workflow_id") or "")
    if not workflow_id:
        return {"error": "session not linked to a workflow"}
    if classification.decision not in {"approve", "reject"}:
        return {"error": "not an approval gate response"}

    from ts_harness.workflows.engine import get_workflow_engine

    engine = get_workflow_engine()
    status = engine.get_status(workflow_id)
    tasks = list(status.get("tasks") or [])
    actionable = _actionable_pending_approval_ids(tasks)
    gate = next(
        (task for task in tasks if str(task.get("id") or "") in actionable),
        None,
    )
    if gate is None:
        return {"error": "no pending approval gate found"}

    from ts_harness.workflows.decisions import refusal_for_permission, resolve_decision_actor

    actor = resolve_decision_actor(metadata, fallback_actor=agent_id)
    denied = refusal_for_permission(actor, "approve_gate")
    if not denied:
        from ts_harness.gateway.program_access import channel_program_refusal

        denied = channel_program_refusal(
            metadata,
            tenant_id=tenant_id,
            channel=str(metadata.get("channel") or "slack"),
            task_id=str(gate.get("id") or ""),
            route="channel_gate",
        )
    if denied:
        return {"error": denied, "status": "denied", "task_id": str(gate.get("id"))}

    if classification.decision == "approve":
        result = engine.handle_approve(
            workflow_id=workflow_id,
            gate_task=gate,
            actor=agent_id,
            tenant_id=tenant_id,
            reply_text=reply_text,
        )
        return {**(result or {}), "status": "approved", "task_id": str(gate.get("id"))}

    feedback = (classification.feedback or "revision requested").strip()
    result = engine.handle_reject(
        workflow_id=workflow_id,
        gate_task=gate,
        comments=feedback,
        actor=agent_id,
        tenant_id=tenant_id,
    )
    return {**(result or {}), "status": "rejected", "task_id": str(gate.get("id"))}


def execute_workflow_gate_response(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    user_text: str,
    provider: ModelProvider | None = None,
    use_llm_fallback: bool = True,
) -> dict[str, Any]:
    if _repo_pick_reply(session, user_text):
        classification = ConfirmationClassification(decision="approve", source="deterministic")
    else:
        classification = classify_approval_gate_response(
            user_text,
            provider=provider,
            use_llm_fallback=use_llm_fallback,
        )
    if classification.decision == "ambiguous":
        return {
            "status": "ambiguous",
            "summary": gate_clarification_message(session),
        }
    if classification.decision == "none":
        if looks_like_revision_feedback(user_text):
            classification = ConfirmationClassification(
                decision="reject",
                feedback=(user_text or "").strip(),
                source="deterministic",
            )
        else:
            return {"status": "none", "summary": gate_clarification_message(session)}
    return execute_workflow_gate_classification(
        session,
        classification,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
        reply_text=user_text,
    )


def format_approval_gate_clarification() -> str:
    return confirmation_clarification_message("approval_gate")


def format_workflow_gate_response_ack(
    result: dict[str, Any],
    *,
    workflow_type: str = "",
) -> str:
    if result.get("status") == "ambiguous":
        return str(result.get("summary") or format_approval_gate_clarification())
    if result.get("error"):
        return f"I couldn't update the workflow approval gate: {result['error']}"
    if result.get("status") == "approved":
        message = "Approved the current workflow gate. I'll continue the workflow."
        resolved_type = workflow_type or str(result.get("workflow_type") or "")
        if resolved_type:
            try:
                from ts_harness.workflows.materialize import load_skeleton

                skeleton = load_skeleton(resolved_type)
                bridge = dict(getattr(skeleton, "bridge", None) or {})
                if bridge:
                    message = (
                        "PRD approved. How would you like to continue delivery — "
                        'file Jira tickets (e.g. "create jira") or generate work orders '
                        '(e.g. "plan work orders")? You can also ask for workflow status.'
                    )
            except FileNotFoundError:
                pass
        return message
    if result.get("status") == "rejected":
        return "Thanks — I sent your feedback back for revision."
    return "Updated the current workflow gate."


def format_bridge_started_message(result: dict[str, Any]) -> str:
    workflow_type = str(
        result.get("workflow_type")
        or result.get("target_workflow_type")
        or "downstream workflow"
    ).replace("_", " ")
    first_step = str(result.get("first_step") or "next step")
    return (
        f"Started the {workflow_type} workflow. "
        f"The first step ({first_step.replace('_', ' ')}) is queued and will run shortly."
    )


def is_workflow_status_query(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _CONFIRM_ONLY_RE.match(text):
        return False
    if _STATUS_QUERY_RE.search(text):
        return True
    normalized = text.lower()
    return "status" in normalized and len(normalized) <= 80


def should_auto_report_workflow_status(metadata: dict, user_text: str) -> bool:
    if not is_workflow_status_query(user_text):
        return False
    return bool(metadata.get("workflow_id") or metadata.get("pending_workflow"))


def execute_workflow_status_query(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
) -> dict[str, Any]:
    metadata = session.metadata or {}
    workflow_id = metadata.get("workflow_id")
    if not workflow_id:
        pending = metadata.get("pending_workflow")
        if isinstance(pending, dict) and pending.get("workflow_type"):
            return {
                "status": "awaiting_confirmation",
                "workflow_type": pending.get("workflow_type"),
                "summary": pending.get("summary"),
            }
        return {"error": "session not linked to a workflow"}
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
    )
    return workflow_status_handler({}, ctx)


def _actionable_pending_approval_ids(tasks: list[dict[str, Any]]) -> set[str]:
    by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    actionable: set[str] = set()
    for task in tasks:
        if not task.get("approval_required"):
            continue
        if str(task.get("approval_status") or "").lower() != "pending":
            continue
        if str(task.get("status") or "").lower() != "pending":
            continue
        deps = [str(dep_id) for dep_id in (task.get("depends_on") or [])]
        if all(str(by_id.get(dep_id, {}).get("status") or "").lower() == "succeeded" for dep_id in deps):
            actionable.add(str(task["id"]))
    return actionable


def _format_task_status(task: dict[str, Any], *, actionable_gate: bool = False) -> str:
    step = str(task.get("step_name") or "step").replace("_", " ")
    status = str(task.get("status") or "unknown").lower()
    error = str(task.get("error") or "")
    if status == "blocked" and "awaiting_external:channel.intake_response" in error:
        return f"{step}: blocked (awaiting your answers)"
    if actionable_gate and status == "pending":
        return f"{step}: pending (awaiting your approval)"
    return f"{step}: {status}"


def format_workflow_status_message(result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"I couldn't load workflow status: {result['error']}"

    if result.get("status") == "awaiting_confirmation":
        workflow_type = str(result.get("workflow_type") or "workflow").replace("_", " ")
        summary = str(result.get("summary") or "").strip()
        if summary:
            lead = f"Your {workflow_type} workflow is ready but not started yet ({summary})."
        else:
            lead = f"Your {workflow_type} workflow is ready but not started yet."
        return f"{lead}\nReply yes to start, or no to cancel."

    workflow_type = str(result.get("workflow_type") or "workflow").replace("_", " ")
    workflow_status = str(result.get("status") or "running").lower()
    lines = [f"{workflow_type.title()} workflow ({workflow_status}):"]

    tasks = list(result.get("tasks") or [])
    actionable_gate_ids = _actionable_pending_approval_ids(tasks)
    if not tasks:
        lines.append("- No tasks found yet.")
    else:
        for task in tasks:
            task_id = str(task.get("id") or "")
            lines.append(
                f"- {_format_task_status(task, actionable_gate=task_id in actionable_gate_ids)}"
            )

    running = list(result.get("running") or [])
    if not running and tasks and all(str(task.get("status") or "").lower() == "pending" for task in tasks):
        lines.append(
            "Work is still queued. The PRD will appear after intake and draft steps complete."
        )

    if actionable_gate_ids:
        lines.append("Reply yes to approve the current gate, or no with feedback to revise.")

    if any(
        str(task.get("status") or "") == "blocked"
        and "awaiting_external:channel.intake_response" in str(task.get("error") or "")
        for task in tasks
    ):
        lines.append("Reply in this thread with answers to the intake questions to continue.")

    return "\n".join(lines)


def is_workflow_artifact_provenance_query(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _CONFIRM_ONLY_RE.match(text):
        return False
    return bool(_ARTIFACT_PROVENANCE_RE.search(text))


def should_auto_explain_workflow_artifact(metadata: dict, user_text: str) -> bool:
    if not is_workflow_artifact_provenance_query(user_text):
        return False
    if not metadata.get("workflow_id"):
        return False
    from ts_harness.workflows.session_compat import read_specialist_handoff

    return read_specialist_handoff(metadata) is not None


def format_workflow_artifact_provenance_message(metadata: dict[str, Any]) -> str:
    from ts_harness.workflows.session_compat import read_specialist_handoff

    handoff = read_specialist_handoff(metadata) or {}
    step_name = str(handoff.get("step_name") or "workflow step").replace("_", " ")
    agent_id = str(handoff.get("agent_id") or "specialist").replace("_", " ")
    return (
        f"This draft came from the governed workflow: {agent_id} produced it during "
        f"the {step_name} step. Reply yes to approve, or no with feedback to revise."
    )


def execute_workflow_artifact_provenance_query(
    session: SessionRecord,
) -> dict[str, Any]:
    metadata = session.metadata or {}
    from ts_harness.workflows.session_compat import read_specialist_handoff

    handoff = read_specialist_handoff(metadata)
    if not isinstance(handoff, dict):
        return {"error": "no specialist output is linked to this thread yet"}
    return {
        "workflow_id": metadata.get("workflow_id"),
        "workflow_type": metadata.get("workflow_type"),
        "step_name": handoff.get("step_name"),
        "agent_id": handoff.get("agent_id"),
        "summary": format_workflow_artifact_provenance_message(metadata),
    }


def is_workflow_artifact_query(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _CONFIRM_ONLY_RE.match(text):
        return False
    if is_workflow_status_query(text):
        return False
    return bool(_ARTIFACT_QUERY_RE.search(text))


def should_auto_show_workflow_artifact(metadata: dict, user_text: str) -> bool:
    if not is_workflow_artifact_query(user_text):
        return False
    return bool(metadata.get("workflow_id"))


def execute_workflow_artifact_query(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
) -> dict[str, Any]:
    metadata = session.metadata or {}
    workflow_id = metadata.get("workflow_id")
    if not workflow_id:
        return {"error": "session not linked to a workflow"}
    ctx = _channel_policy_context(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source=source,
    )
    status = workflow_status_handler({}, ctx)
    if status.get("error"):
        return status

    workflow_type = str(status.get("workflow_type") or metadata.get("workflow_type") or "")
    tasks = list(status.get("tasks") or [])
    steps: list[str] = []
    display_name = ""
    try:
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton(workflow_type)
        steps = artifact_steps(skeleton)
        display_name = str(getattr(skeleton, "display_name", "") or "")
    except FileNotFoundError:
        steps = []

    artifact, artifact_step = latest_artifact_output(tasks, steps)
    return {
        "workflow_id": workflow_id,
        "workflow_type": workflow_type,
        "display_name": display_name,
        "artifact": artifact,
        "artifact_step": artifact_step,
        "tasks": tasks,
        "artifact_steps": steps,
        "pending_approvals": list(status.get("pending_approvals") or []),
    }


def format_workflow_artifact_message(
    result: dict[str, Any],
    *,
    runtime_config=None,
    session: SessionRecord | None = None,
) -> str:
    if result.get("error"):
        return f"I couldn't load the workflow draft: {result['error']}"

    artifact = result.get("artifact")
    if not isinstance(artifact, dict):
        label = str(result.get("display_name") or result.get("workflow_type") or "draft").replace("_", " ")
        steps = list(result.get("artifact_steps") or [])
        tasks = list(result.get("tasks") or [])
        for step in steps:
            task = next((item for item in tasks if item.get("step_name") == step), None)
            if task and str(task.get("status") or "").lower() in {"pending", "running", "claimed"}:
                return (
                    f"The {label} is still being prepared ({step.replace('_', ' ')}). "
                    "Ask for status to check progress."
                )
        return (
            f"No draft {label} is available yet. "
            "Ask for status to see where the workflow is."
        )

    pending_approvals = list(result.get("pending_approvals") or [])
    present_prompt = "approval" if pending_approvals else "inform"
    if runtime_config is not None:
        from ts_harness.gateway.workflow_presentation import build_workflow_artifact_channel_message

        metadata = (session.metadata or {}) if session is not None else {}
        built = build_workflow_artifact_channel_message(
            artifact,
            display_name=str(result.get("display_name") or "Draft"),
            workflow_type=str(result.get("workflow_type") or ""),
            workflow_id=str(result.get("workflow_id") or metadata.get("workflow_id") or ""),
            step_name=str(result.get("artifact_step") or ""),
            agent_id=str(result.get("agent_id") or ""),
            present_prompt=present_prompt,
            runtime_config=runtime_config,
            session=session,
            source_channel=str(metadata.get("binding_key") or ""),
            share_with_email=str(metadata.get("actor_email") or "") or None,
        )
        return str(built.get("message") or "")

    return format_artifact_for_channel(
        artifact,
        display_name=str(result.get("display_name") or "Draft"),
        present_prompt=present_prompt,
        step_name=str(result.get("artifact_step") or ""),
    )


def should_intercept_tool_approval_reply(metadata: dict[str, Any], user_text: str) -> bool:
    pending = metadata.get("pending_tool_approval")
    if not isinstance(pending, dict) or not pending.get("approval_id"):
        return False
    classification = classify_confirmation(
        user_text,
        context="tool_approval",
        provider=None,
        use_llm_fallback=False,
    )
    return classification.decision in {"approve", "reject"}


def execute_confirmed_tool_approval(
    session: SessionRecord,
    *,
    tenant_id: str,
    agent_id: str,
    source: str,
    user_text: str,
    actor_id: str,
    registry,
    config,
    actor_email: str | None = None,
    channel: str = "",
    auth: Any = None,
) -> dict[str, Any]:
    metadata = dict(session.metadata or {})
    pending = metadata.get("pending_tool_approval") or {}
    approval_id = str(pending.get("approval_id") or "")
    tool_name = str(pending.get("tool_name") or "")
    args = dict(pending.get("args") or {})
    if not approval_id or not tool_name:
        return {"error": "no pending tool approval"}

    classification = classify_confirmation(
        user_text,
        context="tool_approval",
        provider=None,
        use_llm_fallback=False,
    )
    if classification.decision == "ambiguous":
        return {
            "status": "ambiguous",
            "summary": confirmation_clarification_message("tool_approval"),
        }
    if classification.decision == "none":
        return {"status": "none", "error": "not a tool approval response"}
    if classification.decision == "approve" and auth is not None:
        from ts_harness.workflows.decisions import channel_permission_refusal

        denied = channel_permission_refusal(
            metadata,
            channel=channel or source,
            auth=auth,
            permission="approve_gate",
            actor_email=actor_email,
            config=config,
        )
        if denied:
            return {"status": "denied", "error": denied, "tool_name": tool_name}

    if classification.decision == "reject":
        from ts_harness.agent.sessions import get_session_store
        from ts_harness.tools.approvals import get_approval_queue

        get_approval_queue().decide(approval_id, approved=False, actor_id=actor_id)
        metadata.pop("pending_tool_approval", None)
        session.metadata = metadata
        get_session_store().save(session)
        return {"status": "rejected", "tool_name": tool_name}

    from ts_harness.agent.sessions import get_session_store
    from ts_harness.tools.approvals import get_approval_queue
    from ts_harness.tools.policy import evaluate_tool_policy

    queue = get_approval_queue()
    try:
        req = queue.get(approval_id)
    except KeyError:
        return {"error": "approval request not found"}
    if req.status == "pending":
        queue.decide(approval_id, approved=True, actor_id=actor_id)

    agent = config.get_agent(agent_id)
    ctx = _channel_policy_context(session, tenant_id=tenant_id, agent_id=agent_id, source=source)
    ctx.approval_id = approval_id
    if actor_email:
        ctx.actor_email = actor_email
    clean_args = {k: v for k, v in args.items() if k != "approval_id"}
    decision = evaluate_tool_policy(
        tool_name,
        agent,
        config,
        ctx,
        tool_args=clean_args,
    )
    if decision.decision != "allow":
        return {
            "error": decision.reason,
            "decision": decision.decision,
            "policy_id": decision.policy_id,
            "tool_name": tool_name,
        }

    dispatch_args = {**clean_args, "approval_id": approval_id}
    try:
        result = registry.dispatch(tool_name, dispatch_args, context=ctx)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "tool_name": tool_name}

    queue.consume(approval_id)
    metadata.pop("pending_tool_approval", None)
    session.metadata = metadata
    get_session_store().save(session)
    return {"status": "succeeded", "tool_name": tool_name, "result": result}


def format_tool_approval_ack(result: dict[str, Any]) -> str:
    if result.get("status") == "ambiguous":
        return str(result.get("summary") or confirmation_clarification_message("tool_approval"))
    if result.get("status") == "rejected":
        return "Okay — I cancelled the pending action."
    if result.get("status") == "denied":
        return "You need the approve_gate permission to approve this action."
    if result.get("error"):
        return f"I couldn't complete the approved action: {result['error']}"
    tool_result = result.get("result") or {}
    if tool_result.get("sent"):
        return "Done — the email was sent."
    if tool_result.get("written"):
        return "Done — the document was updated."
    if tool_result.get("dryRun"):
        return "Preview ready. Reply yes to proceed with the pending action."
    tool_name = str(result.get("tool_name") or "action").replace("_", " ")
    return f"Done — completed {tool_name}."


_STUCK_COMMAND_RE = re.compile(r"^\s*/(revise|accept|stop)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


def parse_stuck_step_command(user_text: str) -> tuple[str, str] | None:
    """``(action, rest)`` for ``/revise``, ``/accept`` or ``/stop`` replies."""
    match = _STUCK_COMMAND_RE.match(user_text or "")
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


def execute_stuck_step_command(
    session: SessionRecord,
    *,
    user_text: str,
    actor_id: str,
    channel: str,
    auth: Any,
    actor_email: str | None = None,
    config: Any = None,
    memory: Any = None,
    engine: Any = None,
) -> dict[str, Any]:
    """Decide a step of this chat's program that is waiting for a human (revision limit reached)."""
    from ts_harness.workflows import decisions, human_decisions

    parsed = parse_stuck_step_command(user_text)
    if parsed is None:
        return {"status": "none"}
    action, rest = parsed
    metadata = dict(session.metadata or {})
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    if engine is None:
        from ts_harness.workflows.engine import get_workflow_engine

        engine = get_workflow_engine()
    program_id = str(metadata.get("program_id") or "").strip()
    linked = {str(metadata.get(key) or "").strip() for key in ("workflow_id", "active_workflow_id")} - {""}
    candidates: list[dict[str, Any]] = []
    for task in memory.list_stuck_tasks(tenant_id=session.tenant_id) or []:
        summary = human_decisions.stuck_summary(engine, str(task.get("workflow_id") or ""), str(task.get("id") or ""))
        if (program_id and summary.get("program_id") == program_id) or summary.get("workflow_id") in linked:
            candidates.append(summary)
    base = {"action": action}
    if not candidates:
        return {**base, "status": "nothing_waiting"}

    words = rest.split(maxsplit=1)
    chosen = None
    guidance = rest
    if words:
        chosen = next((item for item in candidates if item["work_order"].lower() == words[0].lower()), None)
        if chosen is not None:
            guidance = words[1] if len(words) > 1 else ""
    if chosen is None:
        if len(candidates) > 1:
            labels = [item["work_order"] for item in candidates]
            return {**base, "status": "ambiguous", "error": "several work orders are waiting: " + ", ".join(labels), "work_orders": labels}
        chosen = candidates[0]

    denied = decisions.channel_permission_refusal(
        metadata,
        channel=channel,
        auth=auth,
        permission="approve_gate",
        actor_email=actor_email,
        config=config,
    )
    if not denied:
        from ts_harness.gateway.program_access import channel_program_refusal

        denied = channel_program_refusal(
            metadata,
            tenant_id=session.tenant_id,
            channel=channel,
            auth=auth,
            config=config,
            memory=memory,
            program_id=str(chosen.get("program_id") or "") or None,
            route="stuck_command",
        )
    if denied:
        return {**base, "status": "denied", "error": denied, "work_order": chosen["work_order"]}
    try:
        result = engine.decide_blocked_step(
            chosen["workflow_id"],
            chosen["task_id"],
            action,
            actor=str(actor_email or actor_id or "human"),
            guidance=guidance if action == "revise" else "",
            tenant_id=session.tenant_id,
        )
    except ValueError as exc:
        return {**base, "error": str(exc), "work_order": chosen["work_order"]}
    return {**base, "work_order": chosen["work_order"], **result}


def format_stuck_step_ack(result: dict[str, Any]) -> str:
    action = str(result.get("action") or "revise")
    label = str(result.get("work_order") or "the work order")
    status = result.get("status")
    if status == "nothing_waiting":
        return "Nothing in this program is waiting for a decision right now."
    if status == "ambiguous":
        labels = list(result.get("work_orders") or [])
        examples = " or ".join(f"`/{action} {item}`" for item in labels[:3])
        return f"Several work orders are waiting ({', '.join(labels)}). Say which one, e.g. {examples}."
    if status == "denied":
        error = str(result.get("error") or "")
        if error and not error.startswith("missing permission"):
            return f"I can't do that: {error}"
        return "You need the approve_gate permission to decide this."
    if result.get("error"):
        return f"I couldn't {action} {label}: {result['error']}"
    if status == "revising":
        return (
            f"Sent {label} back to OpenCode with a fresh revision budget "
            f"(revision {result.get('revision_number')}/{result.get('max_revisions')})."
        )
    if status == "accepted":
        return f"Accepted {label} as it is. The delivery continues from the next step."
    if status == "stopped":
        return f"Stopped {label}. Its wave will report it as failed."
    return f"Updated {label}."


def _parked_tool_approval(tasks: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    from ts_harness.tools.approvals import AWAITING_TOOL_APPROVAL_PREFIX

    for task in tasks:
        error = str(task.get("error") or "")
        if str(task.get("status") or "").lower() == "blocked" and error.startswith(
            AWAITING_TOOL_APPROVAL_PREFIX
        ):
            return task, error[len(AWAITING_TOOL_APPROVAL_PREFIX):]
    return None


def should_intercept_workflow_tool_approval_reply(
    metadata: dict[str, Any],
    user_text: str,
    *,
    tasks: list[dict[str, Any]] | None = None,
) -> bool:
    if not metadata.get("workflow_id"):
        return False
    classification = classify_confirmation(
        user_text,
        context="tool_approval",
        provider=None,
        use_llm_fallback=False,
    )
    if classification.decision not in {"approve", "reject"}:
        return False
    if tasks is None:
        try:
            tasks = _workflow_tasks(metadata)
        except Exception:  # noqa: BLE001
            # A memory outage must not swallow a yes meant for another pending confirmation.
            return False
    return _parked_tool_approval(tasks) is not None


def execute_workflow_tool_approval_reply(
    session: SessionRecord,
    *,
    user_text: str,
    actor_id: str,
    channel: str,
    auth: Any,
    actor_email: str | None = None,
    config: Any = None,
    tasks: list[dict[str, Any]] | None = None,
    memory: Any = None,
) -> dict[str, Any]:
    """Decide the tool approval a linked workflow stage is parked on, then requeue it."""
    from ts_harness.tools.approvals import (
        ApprovalAlreadyDecided,
        decide_and_resume,
        get_approval_queue,
    )
    from ts_harness.workflows.decisions import channel_permission_refusal

    metadata = dict(session.metadata or {})
    workflow_id = str(metadata.get("workflow_id") or "")
    task_list = tasks if tasks is not None else _workflow_tasks(metadata)
    parked = _parked_tool_approval(task_list)
    if not workflow_id or parked is None:
        return {"error": "no workflow step is waiting on a tool approval"}
    task, approval_id = parked
    step_name = str(task.get("step_name") or "")

    classification = classify_confirmation(
        user_text,
        context="tool_approval",
        provider=None,
        use_llm_fallback=False,
    )
    if classification.decision not in {"approve", "reject"}:
        return {"status": "none", "error": "not a tool approval response"}

    queue = get_approval_queue()
    try:
        approval = queue.get(approval_id)
    except KeyError:
        return {"error": "approval request not found", "step_name": step_name}
    base = {"tool_name": approval.tool_name, "step_name": step_name, "approval_id": approval_id}
    if approval.workflow_id != workflow_id or approval.task_id != str(task.get("id") or ""):
        return {**base, "error": "approval does not belong to this workflow step"}

    denied = channel_permission_refusal(
        metadata,
        channel=channel,
        auth=auth,
        permission="approve_gate",
        actor_email=actor_email,
        config=config,
    )
    if denied:
        return {**base, "status": "denied", "error": denied}

    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    approved = classification.decision == "approve"
    try:
        approval, resume = decide_and_resume(
            queue,
            memory,
            approval_id,
            approved=approved,
            actor_id=actor_id,
        )
    except ApprovalAlreadyDecided as exc:
        return {**base, "status": "already_decided", "decision": exc.approval.status}
    return {**base, "status": "approved" if approved else "rejected", "resume": resume}


def format_workflow_tool_approval_ack(result: dict[str, Any]) -> str:
    tool = f"`{result.get('tool_name') or 'action'}`"
    step = f"`{result.get('step_name') or 'workflow step'}`"
    status = result.get("status")
    if status == "denied":
        return "You need the approve_gate permission to decide this action."
    if status == "already_decided":
        return f"{tool} was already {result.get('decision') or 'decided'}."
    if result.get("error"):
        return f"I couldn't decide {tool}: {result['error']}"
    verb = "Approved" if status == "approved" else "Rejected"
    resume = result.get("resume") or {}
    if resume.get("status") == "failed":
        return f"{verb} {tool}, but step {step} did not resume: {resume.get('error') or 'unknown error'}"
    if status == "approved":
        return f"Approved {tool}. Resuming step {step}."
    return f"Rejected {tool}. Step {step} will continue without it."
