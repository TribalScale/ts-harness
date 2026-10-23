"""Workflow promotion and gate tool handlers."""

from __future__ import annotations

from typing import Any

from ts_harness.agent.sessions import get_session_store
from ts_harness.gateway.delegate_confirmation import (
    clear_pending_bridge,
    clear_pending_workflow,
    set_pending_bridge,
    set_pending_workflow,
)
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.session_compat import (
    is_bridge_linked,
    read_pending_bridge,
    read_program_id,
    read_program_phase,
    read_source_workflow_id,
    read_workflow_phase,
)
from ts_harness.memory.client import get_memory_client
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.workflows.engine import get_workflow_engine
from ts_harness.gateway.channels.slack_users import channel_requester_actor_id
from ts_harness.workflows.promotion import WorkflowPromotionService, build_binding_key

DEFAULT_PROGRAM_TYPE = "software_delivery"
_LEGACY_PROGRAM_ONLY_WORKFLOWS = frozenset({"governed_delivery"})


def _decision_actor(session, ctx: ToolPolicyContext):
    from ts_harness.workflows.decisions import resolve_decision_actor

    return resolve_decision_actor(
        (session.metadata or {}) if session is not None else {},
        fallback_actor=ctx.agent_id,
        actor_email=ctx.actor_email,
    )


def _session_or_error(ctx: ToolPolicyContext):
    if not ctx.session_id:
        return None, {"error": "session_id required"}
    session = get_session_store().get(ctx.session_id)
    if session is None:
        return None, {"error": "session not found"}
    return session, None


def _linked_workflow_id(session) -> str | None:
    return (session.metadata or {}).get("workflow_id")


def _default_program_type(value: Any = None) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_PROGRAM_TYPE


def program_managed_workflow_types() -> frozenset[str]:
    """Workflow types that must start via program/bridge rather than start_workflow.

    Collects workflow bindings from active/built-in program definitions, then
    excludes free-entry defaults (startable node, no required artifacts, no
    variants) such as ``prd_request`` that remain valid standalone starts.
    """
    from ts_harness.workflows.program_materialize import (
        program_workflow_types_from_definitions,
    )
    from ts_harness.workflows.program_registry import get_program_definition_registry

    registry = get_program_definition_registry()
    registry.reload_if_changed()
    payloads = []
    for program_type in registry.list_types():
        try:
            payloads.append(registry.load(program_type))
        except FileNotFoundError:
            continue
    types = program_workflow_types_from_definitions(payloads)
    free_entry: set[str] = set()
    for payload in payloads:
        startable = {
            str(item).strip()
            for item in list(payload.get("startable_nodes") or [])
            if str(item).strip()
        }
        for node in list(payload.get("nodes") or []):
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "").strip()
            if node_id not in startable:
                continue
            required = list(node.get("required_artifacts") or [])
            variants = list(node.get("workflow_variants") or [])
            default = str(node.get("default_workflow_type") or "").strip()
            if default and not required and not variants:
                free_entry.add(default)
    types.difference_update(free_entry)
    types.update(_LEGACY_PROGRAM_ONLY_WORKFLOWS)
    return frozenset(types)


def is_program_managed_workflow(workflow_type: str) -> bool:
    key = str(workflow_type or "").strip()
    if not key:
        return False
    return key in program_managed_workflow_types()


def __getattr__(name: str):
    # Backward-compatible dynamic alias for callers that still import
    # PHASE_ONLY_WORKFLOW_TYPES; prefer is_program_managed_workflow().
    if name == "PHASE_ONLY_WORKFLOW_TYPES":
        return program_managed_workflow_types()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_program_snapshot(
    *,
    program_type: str | None = None,
    program_id: str | None = None,
) -> dict[str, Any] | None:
    from ts_harness.workflows.program_materialize import parse_program_snapshot
    from ts_harness.workflows.program_registry import get_program_definition_registry

    if program_id:
        try:
            payload = get_memory_client().get_program(program_id)
            program = payload.get("program") or {}
            snapshot = parse_program_snapshot(program.get("program_definition_snapshot"))
            if snapshot is not None:
                return snapshot
        except Exception:
            pass
    key = _default_program_type(program_type)
    try:
        registry = get_program_definition_registry()
        registry.reload_if_changed()
        return registry.load(key)
    except Exception:
        return None


def _runtime_program_defaults() -> dict[str, Any]:
    try:
        from ts_harness.gateway.server import get_runtime

        runtime = get_runtime()
        return dict(getattr(getattr(runtime, "config", None), "program_defaults", None) or {})
    except Exception:
        return {}


def _resolved_config_for_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    from ts_harness.jira.program_config import resolved_program_config

    return resolved_program_config(snapshot, _runtime_program_defaults())


class AmbiguousTrackChoice(ValueError):
    """More than one outgoing edge matched; the user must pick a target."""

    def __init__(self, candidates: list[dict[str, Any]]):
        self.candidates = candidates
        options = ", ".join(
            f"{item.get('target_node')} ({item.get('workflow_type') or 'default'})"
            for item in candidates
        )
        super().__init__(f"ambiguous next step; choose one explicitly: {options}")

    def as_result(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "status": "choice_required",
            "candidates": [
                {
                    "target_node": item.get("target_node"),
                    "workflow_type": item.get("workflow_type"),
                    "matched_signals": item.get("matched_signals") or [],
                }
                for item in self.candidates
            ],
        }


def _resolve_program_bridge_identity(
    metadata: dict[str, Any],
    *,
    summary: str,
    target_workflow_type: str | None = None,
    target_node: str | None = None,
) -> dict[str, str]:
    from ts_harness.workflows.program_materialize import (
        find_edge,
        node_id_for_workflow_type,
        resolve_bridge_candidates_from_text,
        resolve_bridge_target_nodes_from_text,
        select_workflow_type,
    )

    pending_program = metadata.get("pending_program")
    pending_program_type = (
        pending_program.get("program_type")
        if isinstance(pending_program, dict)
        else None
    )
    program_type = _default_program_type(
        metadata.get("program_type") or pending_program_type
    )
    program_id = read_program_id(metadata)
    source_node = str(read_program_phase(metadata) or "").strip()
    workflow_type = str(metadata.get("workflow_type") or "")
    snapshot = _load_program_snapshot(program_type=program_type, program_id=program_id or None)
    if snapshot is not None and not source_node and workflow_type:
        source_node = node_id_for_workflow_type(snapshot, workflow_type) or ""

    resolved_target_node = str(target_node or "").strip()
    resolved_target_workflow = str(target_workflow_type or "").strip()

    if snapshot is not None and source_node:
        if not resolved_target_node and resolved_target_workflow:
            resolved_target_node = node_id_for_workflow_type(snapshot, resolved_target_workflow) or ""
        if not resolved_target_node:
            candidates = resolve_bridge_candidates_from_text(
                summary,
                source_workflow_type=workflow_type or "prd_request",
                snapshot=snapshot,
                source_node=source_node,
            )
            if len(candidates) > 1:
                raise AmbiguousTrackChoice(candidates)
            node_from_text, workflow_from_text = resolve_bridge_target_nodes_from_text(
                summary,
                source_workflow_type=workflow_type or "prd_request",
                snapshot=snapshot,
                source_node=source_node,
            )
            if node_from_text:
                resolved_target_node = node_from_text
            if workflow_from_text and not resolved_target_workflow:
                resolved_target_workflow = workflow_from_text
        if resolved_target_node and not resolved_target_workflow:
            try:
                resolved_target_workflow = select_workflow_type(
                    snapshot,
                    resolved_target_node,
                    {},
                )
            except KeyError:
                resolved_target_workflow = ""
        if resolved_target_node and find_edge(
            snapshot,
            source=source_node,
            target=resolved_target_node,
        ) is None:
            raise ValueError(
                f"cannot bridge from {source_node} to {resolved_target_node} "
                f"for program_type={program_type}"
            )

    return {
        "program_type": program_type,
        "source_node": source_node,
        "target_node": resolved_target_node,
        "target_workflow_type": resolved_target_workflow,
    }


def _reject_stale_program_advance(
    metadata: dict[str, Any],
    pending: dict[str, Any],
) -> str | None:
    source_node = str(pending.get("source_node") or "").strip()
    target_node = str(pending.get("target_node") or pending.get("target_phase") or "").strip()
    if not source_node and not target_node:
        return None
    current = str(read_program_phase(metadata) or "").strip()
    if source_node and current and source_node != current:
        return (
            f"stale program advance confirmation: expected source_node={source_node}, "
            f"current program_phase={current}"
        )
    program_type = _default_program_type(
        pending.get("program_type") or metadata.get("program_type")
    )
    program_id = str(pending.get("program_id") or read_program_id(metadata) or "")
    snapshot = _load_program_snapshot(program_type=program_type, program_id=program_id or None)
    if snapshot is None or not source_node or not target_node:
        return None
    from ts_harness.workflows.program_materialize import find_edge

    if find_edge(snapshot, source=source_node, target=target_node) is None:
        return (
            f"impossible program advance from {source_node} to {target_node} "
            f"for program_type={program_type}"
        )
    return None


def _resolve_gate_task(workflow_id: str, task_id: str | None = None) -> dict[str, Any] | None:
    client = get_memory_client()
    tasks = list(client.get_workflow(workflow_id).get("tasks") or [])
    if task_id:
        return next((task for task in tasks if str(task.get("id")) == task_id), None)
    pending = [
        task
        for task in tasks
        if task.get("approval_required")
        and task.get("approval_status") == "pending"
        and task.get("status") == "pending"
    ]
    return pending[0] if pending else None


def propose_start_workflow_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    workflow_type = str(args.get("workflow_type", "")).strip()
    summary = str(args.get("summary", "")).strip()
    if not workflow_type or not summary:
        return {"error": "workflow_type and summary are required"}
    if _linked_workflow_id(session):
        return {"error": "session already linked to a workflow"}
    program_type = _default_program_type(args.get("program_type"))
    pending = (session.metadata or {}).get("pending_workflow") or {}
    if pending.get("workflow_type") == workflow_type:
        return {
            "status": "awaiting_confirmation",
            "workflow_type": workflow_type,
            "program_type": str(pending.get("program_type") or program_type),
            "summary": str(pending.get("summary") or summary),
            "message": "Ask the user to confirm before start_workflow runs.",
        }
    input_data = args.get("input") if isinstance(args.get("input"), dict) else {}
    set_pending_workflow(
        session,
        workflow_type=workflow_type,
        summary=summary,
        input_data=input_data,
        program_type=program_type,
    )
    return {
        "status": "awaiting_confirmation",
        "workflow_type": workflow_type,
        "program_type": program_type,
        "summary": summary,
        "message": "Ask the user to confirm before start_workflow runs.",
    }


def start_workflow_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    pending = (session.metadata or {}).get("pending_workflow") or {}
    workflow_type = str(args.get("workflow_type") or pending.get("workflow_type") or "").strip()
    summary = str(args.get("summary") or pending.get("summary") or "").strip()
    if not workflow_type or not summary:
        return {"error": "workflow_type and summary are required"}
    channel = str((session.metadata or {}).get("channel") or ctx.source)
    channel_id = str((session.metadata or {}).get("channel_id") or channel)
    thread_id = str((session.metadata or {}).get("thread_id") or session.session_id)
    binding_key = build_binding_key(channel, channel_id, thread_id)
    from ts_harness.agent.transcript import get_transcript_store

    history = get_transcript_store().load_messages(session.session_id)
    session_metadata = session.metadata or {}
    requester_actor_id = channel_requester_actor_id(session_metadata, ctx.agent_id)
    service = WorkflowPromotionService()
    try:
        result = service.start(
            session=session,
            workflow_type=workflow_type,
            summary=summary,
            tenant_id=ctx.tenant_id,
            binding_key=binding_key,
            requester_actor_id=requester_actor_id,
            transcript_messages=history,
            extra_input=pending.get("input") if isinstance(pending.get("input"), dict) else None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    clear_pending_workflow(session)
    return {
        "status": "started",
        "workflow_id": result.workflow_id,
        "workflow_type": result.workflow_type,
        "first_step": result.first_step,
        "binding_key": result.binding_key,
        "task_ids": result.task_ids,
    }


def workflow_status_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    workflow_id = str(args.get("workflow_id") or _linked_workflow_id(session) or "")
    if not workflow_id:
        return {"error": "workflow_id required"}
    return get_workflow_engine().get_status(workflow_id)


def workflow_approve_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    workflow_id = _linked_workflow_id(session)
    if not workflow_id:
        return {"error": "session not linked to a workflow"}
    task_id = args.get("task_id")
    gate = _resolve_gate_task(workflow_id, str(task_id) if task_id else None)
    if gate is None:
        return {"error": "no pending approval gate found"}
    if str(gate.get("workflow_id")) != workflow_id:
        return {"error": "task does not belong to linked workflow"}
    human = _decision_actor(session, ctx)
    return get_workflow_engine().handle_approve(
        workflow_id=workflow_id,
        gate_task=gate,
        actor=human.actor_id,
        tenant_id=ctx.tenant_id,
        decision=human.as_fields(),
    )


def workflow_reject_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    workflow_id = _linked_workflow_id(session)
    if not workflow_id:
        return {"error": "session not linked to a workflow"}
    task_id = args.get("task_id")
    gate = _resolve_gate_task(workflow_id, str(task_id) if task_id else None)
    if gate is None:
        return {"error": "no pending approval gate found"}
    if str(gate.get("workflow_id")) != workflow_id:
        return {"error": "task does not belong to linked workflow"}
    comments = str(args.get("comments") or args.get("reason") or "revision requested")
    human = _decision_actor(session, ctx)
    return get_workflow_engine().handle_reject(
        workflow_id=workflow_id,
        gate_task=gate,
        comments=comments,
        actor=human.actor_id,
        tenant_id=ctx.tenant_id,
        decision={**human.as_fields(), "reason": comments},
    )


def workflow_reject_with_feedback_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    comments = str(args.get("comments") or "").strip()
    if not comments:
        return {"error": "comments are required"}
    payload = dict(args)
    payload["comments"] = comments
    return workflow_reject_handler(payload, ctx)


def cancel_workflow_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    workflow_id = str(args.get("workflow_id") or _linked_workflow_id(session) or "")
    if not workflow_id:
        return {"error": "workflow_id required"}
    reason = str(args.get("reason") or "cancelled by user")
    return get_workflow_engine().cancel(workflow_id, actor=ctx.agent_id, tenant_id=ctx.tenant_id, reason=reason)


def propose_bridge_workflow_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    metadata = session.metadata or {}
    if is_bridge_linked(metadata):
        return {"error": "session already linked to bridge target workflow"}
    workflow_type = str(metadata.get("workflow_type") or "")
    skeleton = load_skeleton(workflow_type) if workflow_type else None
    bridge = dict(getattr(skeleton, "bridge", None) or {}) if skeleton else {}
    program_id = read_program_id(metadata)
    if not bridge and not program_id:
        return {"error": "no bridge configured for linked workflow"}
    approved = read_workflow_phase(metadata)
    tracking = None
    if skeleton is not None:
        from ts_harness.workflows.workflow_session import get_phase_tracking

        tracking = get_phase_tracking(skeleton)
    approved_value = tracking.approved_value if tracking else "approved"
    if approved != approved_value:
        return {"error": "workflow must be approved before bridging"}
    source_workflow_id = read_source_workflow_id(metadata)
    if not source_workflow_id:
        return {"error": "no approved source workflow linked to session"}
    pending = read_pending_bridge(metadata) or {}
    summary = str(args.get("summary") or pending.get("summary") or "").strip()
    if not summary:
        return {"error": "summary is required"}
    target_workflow_type = str(
        args.get("target_workflow_type")
        or pending.get("target_workflow_type")
        or bridge.get("target_workflow_type")
        or ""
    )
    program_type = None
    source_node = None
    target_node = None
    if program_id:
        try:
            identity = _resolve_program_bridge_identity(
                metadata,
                summary=summary,
                target_workflow_type=target_workflow_type or None,
                target_node=str(args.get("target_node") or pending.get("target_node") or "") or None,
            )
        except AmbiguousTrackChoice as exc:
            return exc.as_result()
        except ValueError as exc:
            return {"error": str(exc)}
        program_type = identity["program_type"]
        source_node = identity["source_node"] or None
        target_node = identity["target_node"] or None
        target_workflow_type = identity["target_workflow_type"] or target_workflow_type
    elif not target_workflow_type:
        from ts_harness.workflows.delivery_program import resolve_bridge_target_from_text

        target_workflow_type = resolve_bridge_target_from_text(summary, workflow_type) or ""
    set_pending_bridge(
        session,
        source_workflow_id=source_workflow_id,
        summary=summary,
        target_workflow_type=target_workflow_type or None,
        program_type=program_type,
        source_node=source_node,
        target_node=target_node,
    )
    result = {
        "status": "awaiting_confirmation",
        "source_workflow_id": source_workflow_id,
        "target_workflow_type": target_workflow_type,
        "summary": summary,
        "message": "Ask the user to confirm before bridge_workflow runs.",
    }
    if program_type:
        result["program_type"] = program_type
    if source_node:
        result["source_node"] = source_node
    if target_node:
        result["target_node"] = target_node
    return result


def propose_continue_delivery_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    return propose_bridge_workflow_handler(args, ctx)


def bridge_workflow_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    metadata = session.metadata or {}
    pending = read_pending_bridge(metadata) or {}
    summary = str(args.get("summary") or pending.get("summary") or "").strip()
    if not summary:
        return {"error": "summary is required"}
    workflow_type = str(metadata.get("workflow_type") or "")
    target_workflow_type = str(
        args.get("target_workflow_type") or pending.get("target_workflow_type") or "",
    ).strip() or None
    target_node = str(args.get("target_node") or pending.get("target_node") or "").strip() or None
    if read_program_id(metadata):
        stale = _reject_stale_program_advance(
            metadata,
            {
                "source_node": pending.get("source_node") or read_program_phase(metadata),
                "target_node": target_node,
                "program_type": pending.get("program_type") or metadata.get("program_type"),
                "program_id": read_program_id(metadata),
            },
        )
        if stale and target_node:
            return {"error": stale}
        try:
            identity = _resolve_program_bridge_identity(
                metadata,
                summary=summary,
                target_workflow_type=target_workflow_type,
                target_node=target_node,
            )
        except AmbiguousTrackChoice as exc:
            return exc.as_result()
        except ValueError as exc:
            return {"error": str(exc)}
        target_workflow_type = identity["target_workflow_type"] or target_workflow_type
        target_node = identity["target_node"] or target_node
    if not target_workflow_type:
        from ts_harness.workflows.delivery_program import resolve_bridge_target_from_text

        target_workflow_type = resolve_bridge_target_from_text(
            summary,
            workflow_type,
            program_type=_default_program_type(
                pending.get("program_type") or metadata.get("program_type")
            ),
            source_node=str(pending.get("source_node") or read_program_phase(metadata) or "") or None,
        )
    try:
        human = _decision_actor(session, ctx)
        result = get_workflow_engine().bridge_workflow(
            session=session,
            actor=human.actor_id,
            tenant_id=ctx.tenant_id,
            summary=summary,
            target_workflow_type=target_workflow_type,
            target_node=target_node,
            decision=human.as_fields(),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    clear_pending_bridge(session)
    return result


def continue_delivery_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    return bridge_workflow_handler(args, ctx)


def resume_external_event_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    workflow_id = str(args.get("workflow_id") or _linked_workflow_id(session) or "")
    if not workflow_id:
        return {"error": "workflow_id required"}
    event_type = str(args.get("event_type") or "").strip()
    if not event_type:
        return {"error": "event_type is required"}
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    return get_workflow_engine().resume_external(
        workflow_id,
        event_type=event_type,
        payload=payload,
        actor=ctx.agent_id,
        tenant_id=ctx.tenant_id,
    )


def remember_memory_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    content = str(args.get("content") or "").strip()
    if not content:
        return {"error": "content is required"}
    return get_memory_client().remember(
        tenant_id=ctx.tenant_id,
        agent_id=ctx.agent_id,
        content=content,
        memory_type=str(args.get("type") or "semantic"),
        tags=list(args.get("tags") or []),
    )


def sessions_list_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    source = args.get("source")
    sessions = get_session_store().list_for_tenant(ctx.tenant_id)
    if source:
        sessions = [session for session in sessions if session.source == str(source)]
    return {
        "sessions": [
            {
                "session_id": session.session_id,
                "agent_id": session.agent_id,
                "source": session.source,
                "status": session.status,
                "metadata": session.metadata,
            }
            for session in sessions
        ]
    }


def sessions_history_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    from ts_harness.agent.transcript import get_transcript_store

    session_id = str(args.get("session_id") or ctx.session_id or "")
    if not session_id:
        return {"error": "session_id required"}
    session = get_session_store().get(session_id)
    if session is None or session.tenant_id != ctx.tenant_id:
        return {"error": "session not found"}
    messages = get_transcript_store().load_messages(session_id)
    return {"session_id": session_id, "messages": messages}


def search_sessions_handler(
    args: dict[str, Any], ctx: ToolPolicyContext
) -> dict[str, Any]:
    from dataclasses import asdict

    from ts_harness.agent.transcript import get_transcript_store

    query = str(args.get("query") or "")
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    hits = get_transcript_store().search(
        tenant_id=str(ctx.tenant_id or ""),
        query=query,
        limit=limit,
    )
    return {"hits": [asdict(hit) for hit in hits]}


def propose_start_program_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    entry_phase = str(args.get("entry_phase") or "").strip()
    summary = str(args.get("summary") or "").strip()
    if not entry_phase or not summary:
        return {"error": "entry_phase and summary are required"}
    if _linked_workflow_id(session) and not (session.metadata or {}).get("program_id"):
        return {"error": "session already linked to a workflow"}
    program_type = _default_program_type(args.get("program_type"))
    input_data = args.get("input") if isinstance(args.get("input"), dict) else {}
    artifacts = args.get("artifacts") if isinstance(args.get("artifacts"), dict) else input_data
    from ts_harness.workflows.program_materialize import validate_entry_against_snapshot

    snapshot = _load_program_snapshot(program_type=program_type)
    if snapshot is not None:
        errors = validate_entry_against_snapshot(
            snapshot,
            entry_phase,
            artifacts or {},
            require_startable=True,
        )
    else:
        from ts_harness.workflows.delivery_program import get_delivery_program_service

        errors = get_delivery_program_service().validate_entry(entry_phase, artifacts or {})
    if errors:
        return {"error": "; ".join(errors)}
    pending = (session.metadata or {}).get("pending_program") or {}
    if pending.get("entry_phase") == entry_phase and _default_program_type(
        pending.get("program_type")
    ) == program_type:
        return {
            "status": "awaiting_confirmation",
            "entry_phase": entry_phase,
            "program_type": program_type,
            "summary": str(pending.get("summary") or summary),
            "project_id": pending.get("project_id"),
            "project_id_source": pending.get("project_id_source"),
            "project_id_inferred": pending.get("project_id_source")
            in {"inferred_jira", "inferred_repo"},
            "link_program_ids": pending.get("link_program_ids") or [],
            "resolved_config": _resolved_config_for_snapshot(snapshot),
            "message": "Ask the user to confirm before start_program runs.",
        }
    from ts_harness.gateway.delegate_confirmation import set_pending_program
    from ts_harness.workflows.project_identity import resolve_program_project

    user_project = args.get("project_id") or pending.get("project_id")
    project_id, project_id_source = resolve_program_project(
        artifacts,
        project_id=str(user_project).strip() if user_project else None,
    )
    link_program_ids = args.get("link_program_ids") or pending.get("link_program_ids") or []
    if not isinstance(link_program_ids, list):
        link_program_ids = []

    set_pending_program(
        session,
        entry_phase=entry_phase,
        summary=summary,
        artifacts=artifacts,
        program_type=program_type,
        project_id=project_id,
        project_id_source=project_id_source,
        link_program_ids=link_program_ids,
    )
    inferred = project_id_source in {"inferred_jira", "inferred_repo"}
    project_note = (
        f" Inferred project is {project_id!r}. Override with project_id or add link_program_ids before start_program."
        if project_id
        else " No project_id inferred (Jira key or repo). Set project_id to enable auto-share, or pass link_program_ids."
    )
    return {
        "status": "awaiting_confirmation",
        "entry_phase": entry_phase,
        "program_type": program_type,
        "summary": summary,
        "project_id": project_id,
        "project_id_source": project_id_source,
        "project_id_inferred": inferred,
        "link_program_ids": link_program_ids,
        "parent_program_id": resolve_start_parent_program(
            program_type,
            explicit=args.get("parent_program_id"),
            session_metadata=session.metadata or {},
        ),
        "resolved_config": _resolved_config_for_snapshot(snapshot),
        "message": "Ask the user to confirm before start_program runs." + project_note,
    }


CHILD_PROGRAM_PARENT_TYPES = {
    "content_to_jira_work_orders": {"software_delivery"},
    "bug_to_jira_work_orders": {"software_delivery"},
}


def resolve_start_parent_program(
    program_type: str,
    *,
    explicit: Any,
    session_metadata: dict[str, Any],
) -> str | None:
    """Pick the parent for a new program: explicit id, else the thread's owning program."""
    text = str(explicit or "").strip()
    if text:
        return text
    parent_types = CHILD_PROGRAM_PARENT_TYPES.get(program_type)
    if not parent_types:
        return None
    current_id = str(session_metadata.get("program_id") or "").strip()
    current_type = str(session_metadata.get("program_type") or "").strip()
    if current_id and current_type in parent_types:
        return current_id
    return None


def start_program_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    pending = (session.metadata or {}).get("pending_program") or {}
    entry_phase = str(args.get("entry_phase") or pending.get("entry_phase") or "").strip()
    summary = str(args.get("summary") or pending.get("summary") or "").strip()
    if not entry_phase or not summary:
        return {"error": "entry_phase and summary are required"}
    program_type = _default_program_type(args.get("program_type") or pending.get("program_type"))
    artifacts = args.get("artifacts") if isinstance(args.get("artifacts"), dict) else {}
    if not artifacts and isinstance(pending.get("artifacts"), dict):
        artifacts = pending["artifacts"]
    channel = str((session.metadata or {}).get("channel") or ctx.source)
    channel_id = str((session.metadata or {}).get("channel_id") or channel)
    thread_id = str((session.metadata or {}).get("thread_id") or session.session_id)
    binding_key = build_binding_key(channel, channel_id, thread_id)
    from ts_harness.agent.transcript import get_transcript_store
    from ts_harness.gateway.delegate_confirmation import clear_pending_program
    from ts_harness.workflows.delivery_program import get_delivery_program_service

    history = get_transcript_store().load_messages(session.session_id)
    service = get_delivery_program_service()
    from ts_harness.workflows.project_identity import resolve_program_project

    user_project = args.get("project_id") or pending.get("project_id")
    project_id, project_id_source = resolve_program_project(
        artifacts,
        project_id=str(user_project).strip() if user_project else None,
    )
    link_program_ids = args.get("link_program_ids") or pending.get("link_program_ids") or []
    if not isinstance(link_program_ids, list):
        link_program_ids = []
    parent_program_id = resolve_start_parent_program(
        program_type,
        explicit=args.get("parent_program_id") or pending.get("parent_program_id"),
        session_metadata=session.metadata or {},
    )
    department = None
    starter_email = str(getattr(ctx, "actor_email", None) or "").strip()
    if starter_email:
        try:
            from ts_harness.gateway.server import get_runtime
            from ts_harness.policy.permissions import department_for_email

            department = department_for_email(get_runtime().config, starter_email)
        except Exception:
            department = None
    try:
        result = service.create_program(
            entry_phase=entry_phase,
            summary=summary,
            tenant_id=ctx.tenant_id,
            binding_key=binding_key,
            requester_actor_id=channel_requester_actor_id(session.metadata or {}, ctx.agent_id),
            artifacts=artifacts,
            transcript_messages=history,
            actor=ctx.agent_id,
            program_type=program_type,
            project_id=project_id,
            project_id_source=project_id_source,
            link_program_ids=[str(item) for item in link_program_ids],
            parent_program_id=parent_program_id,
            department=department,
            owner_id=str((session.metadata or {}).get("directory_subject") or "") or None,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    metadata = dict(session.metadata or {})
    from ts_harness.workflows.session_compat import write_program_session

    metadata = write_program_session(
        metadata,
        program_id=result.program_id,
        program_phase=result.entry_phase,
        workflow_id=result.workflow_id,
        artifacts=artifacts,
    )
    metadata.update(
        {
            "workflow_type": result.workflow_type,
            "program_type": program_type,
            "binding_key": binding_key,
            "mode": "workflow_linked",
            "pending_workflow": None,
        },
    )
    session.metadata = metadata
    get_session_store().save(session)
    clear_pending_program(session)

    return {
        "status": "started",
        "program_id": result.program_id,
        "program_type": program_type,
        "entry_phase": result.entry_phase,
        "workflow_id": result.workflow_id,
        "workflow_type": result.workflow_type,
        "first_step": result.first_step,
        "task_ids": result.task_ids,
        "binding_key": binding_key,
        "resolved_config": _resolved_config_for_snapshot(
            _load_program_snapshot(program_type=program_type, program_id=result.program_id)
        ),
    }


class ProgramLookupError(Exception):
    """A program ref did not resolve to exactly one id."""


def resolve_program_ref(client: Any, ref: str, tenant_id: str) -> str:
    """Return a program id for an id or a unique display name."""
    from ts_harness.memory.client import ProgramNotFound

    cleaned = str(ref or "").strip()
    if not cleaned:
        return cleaned
    try:
        client.get_program(cleaned)
        return cleaned
    except ProgramNotFound:
        pass
    except Exception:
        if not any(ch.isspace() for ch in cleaned):
            return cleaned
    listed = client.list_programs(tenant_id=tenant_id, limit=100)
    programs = listed.get("programs") if isinstance(listed, dict) else []
    needle = cleaned.casefold()
    matches: list[str] = []
    for program in programs or []:
        if not isinstance(program, dict) or not program.get("id"):
            continue
        names = {
            str(program.get("display_name") or "").strip().casefold(),
            str(program.get("summary") or "").strip().casefold(),
        }
        if needle in names:
            matches.append(str(program["id"]))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ProgramLookupError(f"program not found: {cleaned}")
    raise ProgramLookupError(f"program name is ambiguous: {cleaned}")


def program_status_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    program_id = str(args.get("program_id") or (session.metadata or {}).get("program_id") or "")
    if not program_id:
        return {"error": "program_id required"}
    try:
        program_id = resolve_program_ref(
            get_memory_client(),
            program_id,
            str(getattr(session, "tenant_id", None) or ctx.tenant_id or "default"),
        )
    except ProgramLookupError as exc:
        return {"error": str(exc)}
    from ts_harness.workflows.delivery_program import get_delivery_program_service

    status = get_delivery_program_service().get_status(program_id)
    if isinstance(status, dict):
        program_type = str(
            args.get("program_type")
            or (session.metadata or {}).get("program_type")
            or ""
        )
        status = dict(status)
        status["resolved_config"] = _resolved_config_for_snapshot(
            _load_program_snapshot(program_type=program_type or None, program_id=program_id)
        )
    return status


def propose_advance_program_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    metadata = session.metadata or {}
    program_id = str(metadata.get("program_id") or args.get("program_id") or "")
    if not program_id:
        return {"error": "session not linked to a delivery program"}
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return {"error": "summary is required"}
    target_phase = str(args.get("target_phase") or args.get("target_node") or "").strip() or None
    program_type = _default_program_type(
        args.get("program_type") or metadata.get("program_type")
    )
    source_node = str(read_program_phase(metadata) or "").strip() or None
    target_node = target_phase
    if not target_node:
        try:
            identity = _resolve_program_bridge_identity(
                metadata,
                summary=summary,
            )
            target_node = identity["target_node"] or None
            target_phase = target_node
            program_type = identity["program_type"] or program_type
            source_node = identity["source_node"] or source_node
        except AmbiguousTrackChoice as exc:
            return exc.as_result()
        except ValueError as exc:
            return {"error": str(exc)}
    from ts_harness.gateway.delegate_confirmation import set_pending_program_advance

    set_pending_program_advance(
        session,
        program_id=program_id,
        summary=summary,
        target_phase=target_phase,
        program_type=program_type,
        source_node=source_node,
        target_node=target_node,
    )
    return {
        "status": "awaiting_confirmation",
        "program_id": program_id,
        "program_type": program_type,
        "source_node": source_node,
        "target_node": target_node,
        "target_phase": target_phase,
        "summary": summary,
        "message": "Ask the user to confirm before advance_program runs.",
    }


def advance_program_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    metadata = session.metadata or {}
    pending = metadata.get("pending_program_advance") or {}
    program_id = str(args.get("program_id") or metadata.get("program_id") or pending.get("program_id") or "")
    if not program_id:
        return {"error": "program_id required"}
    summary = str(args.get("summary") or pending.get("summary") or "").strip()
    if not summary:
        return {"error": "summary is required"}
    target_phase = str(
        args.get("target_phase")
        or args.get("target_node")
        or pending.get("target_node")
        or pending.get("target_phase")
        or ""
    ).strip() or None
    artifacts = args.get("artifacts") if isinstance(args.get("artifacts"), dict) else {}
    if metadata.get("approved_artifact") and "approved_prd" not in artifacts:
        artifacts["approved_prd"] = metadata.get("approved_artifact")

    stale = _reject_stale_program_advance(metadata, pending)
    if stale:
        return {"error": stale}

    if program_id and not target_phase and read_workflow_phase(metadata) == "approved":
        return bridge_workflow_handler({"summary": summary}, ctx)

    channel = str(metadata.get("channel") or ctx.source)
    channel_id = str(metadata.get("channel_id") or channel)
    thread_id = str(metadata.get("thread_id") or session.session_id)
    binding_key = build_binding_key(channel, channel_id, thread_id)
    from ts_harness.gateway.delegate_confirmation import clear_pending_program_advance
    from ts_harness.workflows.delivery_program import get_delivery_program_service

    service = get_delivery_program_service()
    human = _decision_actor(session, ctx)
    try:
        result = service.advance_program(
            program_id=program_id,
            target_phase=target_phase,
            summary=summary,
            artifacts=artifacts or None,
            tenant_id=ctx.tenant_id,
            binding_key=binding_key,
            requester_actor_id=human.actor_id,
            source_workflow_id=str(metadata.get("workflow_id") or ""),
            actor=human.actor_id,
            decision=human.as_fields(),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    from ts_harness.workflows.session_compat import write_program_session, write_session_mode

    updated = write_session_mode(metadata, "bridge_linked")
    updated = write_program_session(
        updated,
        program_id=program_id,
        program_phase=str(result.get("target_phase") or ""),
        workflow_id=str(result["workflow_id"]),
        artifacts=artifacts or None,
    )
    program_type = _default_program_type(
        pending.get("program_type") or metadata.get("program_type")
    )
    updated.update(
        {
            "workflow_id": result["workflow_id"],
            "workflow_type": result["workflow_type"],
            "program_type": program_type,
        },
    )
    session.metadata = updated
    get_session_store().save(session)
    clear_pending_program_advance(session)
    return result


def _session_program_id(session, args: dict[str, Any]) -> str:
    metadata = session.metadata or {}
    return str(
        args.get("program_id")
        or args.get("from_program_id")
        or metadata.get("program_id")
        or read_program_id(metadata)
        or ""
    ).strip()


def propose_set_program_project_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    program_id = _session_program_id(session, args)
    if not program_id:
        return {"error": "session not linked to a delivery program"}
    try:
        from ts_harness.workflows.project_identity import normalize_project_id

        project_id = normalize_project_id(args.get("project_id"))
    except ValueError as exc:
        return {"error": str(exc)}
    metadata = dict(session.metadata or {})
    metadata["pending_program_project"] = {
        "program_id": program_id,
        "project_id": project_id,
    }
    session.metadata = metadata
    get_session_store().save(session)
    return {
        "status": "awaiting_confirmation",
        "program_id": program_id,
        "project_id": project_id,
        "message": "Ask the user to confirm before set_program_project runs.",
    }


def set_program_project_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    pending = (session.metadata or {}).get("pending_program_project") or {}
    program_id = _session_program_id(session, {**pending, **args})
    raw_project = args.get("project_id") or pending.get("project_id")
    if not program_id:
        return {"error": "program_id required"}
    try:
        from ts_harness.workflows.project_identity import normalize_project_id

        project_id = normalize_project_id(raw_project)
    except ValueError as exc:
        return {"error": str(exc)}
    client = get_memory_client()
    result = client.patch_program(program_id, tenant_id=ctx.tenant_id, project_id=project_id, project_id_source="user", actor=ctx.agent_id)
    from ts_harness.workflows.program_brief import attach_related_program_context

    attach_related_program_context(
        client,
        program_id=program_id,
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        actor=ctx.agent_id,
    )
    metadata = dict(session.metadata or {})
    metadata["pending_program_project"] = None
    session.metadata = metadata
    get_session_store().save(session)
    return {"program_id": program_id, "project_id": project_id, "program": result}


def propose_link_program_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    from_id = _session_program_id(session, args)
    to_id = str(args.get("to_program_id") or "").strip()
    if not from_id:
        return {"error": "session not linked to a delivery program"}
    if not to_id:
        return {"error": "to_program_id is required"}
    metadata = dict(session.metadata or {})
    metadata["pending_program_link"] = {"from_program_id": from_id, "to_program_id": to_id}
    session.metadata = metadata
    get_session_store().save(session)
    return {
        "status": "awaiting_confirmation",
        "from_program_id": from_id,
        "to_program_id": to_id,
        "message": "Ask the user to confirm before link_program runs.",
    }


def link_program_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    pending = (session.metadata or {}).get("pending_program_link") or {}
    from_id = str(args.get("from_program_id") or pending.get("from_program_id") or _session_program_id(session, args) or "")
    to_id = str(args.get("to_program_id") or pending.get("to_program_id") or "")
    if not from_id or not to_id:
        return {"error": "from_program_id and to_program_id are required"}
    client = get_memory_client()
    try:
        result = client.create_program_link(
            from_id,
            to_program_id=to_id,
            tenant_id=ctx.tenant_id,
            actor=ctx.agent_id,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    payload = client.get_program(from_id)
    program = (payload or {}).get("program") or {}
    from ts_harness.workflows.program_brief import attach_related_program_context

    attach_related_program_context(
        client,
        program_id=from_id,
        tenant_id=ctx.tenant_id,
        project_id=program.get("project_id"),
        actor=ctx.agent_id,
    )
    metadata = dict(session.metadata or {})
    metadata["pending_program_link"] = None
    session.metadata = metadata
    get_session_store().save(session)
    return result


def unlink_program_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    from_id = _session_program_id(session, args)
    to_id = str(args.get("to_program_id") or "").strip()
    if not from_id or not to_id:
        return {"error": "from_program_id and to_program_id are required"}
    client = get_memory_client()
    try:
        result = client.delete_program_link(
            from_id,
            to_id,
            tenant_id=ctx.tenant_id,
            actor=ctx.agent_id,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    from ts_harness.workflows.program_brief import rebuild_related_program_context

    rebuild_related_program_context(
        client,
        program_id=from_id,
        tenant_id=ctx.tenant_id,
        actor=ctx.agent_id,
    )
    return result


def set_program_autoshare_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    session, err = _session_or_error(ctx)
    if err:
        return err
    program_id = _session_program_id(session, args)
    if not program_id:
        return {"error": "session not linked to a delivery program"}
    if "excluded" not in args:
        return {"error": "excluded is required"}
    excluded = bool(args.get("excluded"))
    client = get_memory_client()
    result = client.patch_program(
        program_id,
        tenant_id=ctx.tenant_id,
        excluded_from_autoshare=excluded,
        actor=ctx.agent_id,
    )
    payload = client.get_program(program_id)
    program = (payload or {}).get("program") or {}
    from ts_harness.workflows.program_brief import attach_related_program_context

    attach_related_program_context(
        client,
        program_id=program_id,
        tenant_id=ctx.tenant_id,
        project_id=program.get("project_id"),
        actor=ctx.agent_id,
    )
    return {"program_id": program_id, "excluded_from_autoshare": excluded, "program": result}
