"""Enterprise agent tool-use loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
import json
import os
import re
import uuid

from ts_harness.agent.provider import ModelProvider, build_provider
from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.config.schema import AgentConfig, RuntimeConfig
from ts_harness.policy.engine import PolicyEngine
from ts_harness.skills.aliases import canonical_skill_ids
from ts_harness.tools.approvals import AWAITING_TOOL_APPROVAL_PREFIX, get_approval_queue
from ts_harness.tools.audit import get_tool_audit_log
from ts_harness.policy.opa import get_opa_client
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy, redact_tool_error
from ts_harness.tools.registry import ToolRegistry
from ts_harness.workflows.artifacts import (
    WORK_ORDERS_STAGE_ARTIFACT_KEYS,
    artifact_body,
    has_foundry_structural_markers,
    looks_like_invalid_foundry_content,
    looks_like_memory_recall_summary,
    looks_like_prd_recap_text,
    partition_foundry_markdown,
    partition_refinery_markdown,
    work_orders_artifacts_failure_reason,
)
from ts_harness.workflows import stage_contracts

# Cap in-loop repairs so a bad model response can self-correct before the
# runner marks the task blocked / retries the whole dispatch.
_MAX_WORKFLOW_OUTPUT_REPAIRS = 2
_WORKFLOW_MEMORY_TOOLS = frozenset({"recall_memory", "remember_memory", "forget_memory"})


def _ensure_tool_call_id(call: dict[str, Any]) -> str:
    call_id = str(call.get("id") or "")
    if not call_id:
        call_id = str(uuid.uuid4())
        call["id"] = call_id
    return call_id


def _append_tool_message(messages: list[dict[str, Any]], call: dict[str, Any], content: str) -> None:
    messages.append(
        {
            "role": "tool",
            "tool_call_id": _ensure_tool_call_id(call),
            "content": content,
        }
    )


def _close_tool_calls(
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    current: dict[str, Any],
    content: str,
) -> None:
    """Append the current tool result and a skipped result for every later call."""
    start = tool_calls.index(current)
    _append_tool_message(messages, current, content)
    for pending in tool_calls[start + 1 :]:
        _append_tool_message(messages, pending, '{"skipped": true}')


_LAST_UPDATED_RE = re.compile(r"(\*\*Last updated:\*\*[ \t]*)([^·\n]*?)([ \t]*(?:·|$))", re.MULTILINE)


def _finalize_prd_output(output: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp the real date and derive the summary from the PRD, not from the model's talk."""
    result = output.get("result") if isinstance(output.get("result"), dict) else None
    holder = result if result is not None and result.get("prd_markdown") else output
    markdown = holder.get("prd_markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        return output
    today = str((payload.get("input") or {}).get("today") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        today = datetime.now(timezone.utc).date().isoformat()
    stamped = _LAST_UPDATED_RE.sub(lambda m: f"{m.group(1)}{today}{m.group(3)}", markdown, count=1)
    title = str(holder.get("prd_title") or "").strip()
    if not title:
        heading = re.search(r"^#\s+(?:PRD:\s*)?(.+)$", stamped, re.MULTILINE)
        title = heading.group(1).strip() if heading else "PRD"
    profile = str(holder.get("prd_profile") or "").strip().lower()
    verb = "Revised" if str(payload.get("step_name") or "").startswith("revise") else "Drafted"
    summary = f"{verb} PRD: {title}" + (f" ({profile} profile)" if profile in {"lite", "full"} else "")
    output = dict(output)
    if holder is result:
        output["result"] = {**result, "prd_markdown": stamped}
    else:
        output["prd_markdown"] = stamped
    output["summary"] = summary
    return output


def _proposal_formatter(tool_name: str):
    from ts_harness.gateway.channel_confirmations import PROPOSAL_FORMATTERS

    return PROPOSAL_FORMATTERS.get(tool_name)


def with_confirmation_prompt(text: str) -> str:
    from ts_harness.gateway.channel_confirmations import with_confirmation_prompt as _with_prompt

    return _with_prompt(text)


@dataclass
class AgentRunResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    runtime_task_id: str | None = None
    proposed_tasks: list[dict[str, Any]] | None = None
    error: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _soul_paragraph(agent: AgentConfig) -> str:
    soul = str(agent.soul or "").strip()
    if not soul:
        return ""
    return soul[:500] + "\n"


def _stage_tool_allow(payload: dict[str, Any]) -> set[str] | None:
    if "tool_allow" not in payload or payload["tool_allow"] is None:
        return None
    return {str(name) for name in payload["tool_allow"] if str(name or "").strip()}


def _stage_external_effects(payload: dict[str, Any]) -> set[str] | None:
    if "external_effects" not in payload or payload["external_effects"] is None:
        return None
    return {str(name) for name in payload["external_effects"] if str(name or "").strip()}


class EnterpriseAgent:
    def __init__(
        self,
        config: RuntimeConfig,
        registry: ToolRegistry,
        *,
        provider: ModelProvider | None = None,
        max_iterations: int = 8,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
        on_audit: Callable[[dict[str, Any]], None] | None = None,
        on_stream_chunk: Callable[[str], None] | None = None,
        policy_engine: PolicyEngine | None = None,
    ):
        self.config = config
        self.registry = registry
        self.max_iterations = max_iterations
        self.on_tool_call = on_tool_call
        self.on_audit = on_audit
        self.on_stream_chunk = on_stream_chunk
        self.policy_engine = policy_engine
        self._provider_override = provider
        self._interrupt_reason: str | None = None
        self._paused = False

    def interrupt(self, reason: str = "interrupted") -> None:
        self._interrupt_reason = reason

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._interrupt_reason = None

    def run_workflow_dispatch(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        payload: dict[str, Any],
        source: str = "workflow",
        correlation_id: str | None = None,
        is_subagent: bool = False,
        parent_tools: set[str] | None = None,
    ) -> AgentRunResult:
        runtime_task_id = str(uuid.uuid4())
        try:
            agent = self.config.get_agent(agent_id)
        except KeyError as exc:
            return AgentRunResult(status="blocked", error=str(exc), runtime_task_id=runtime_task_id)

        step_name = payload.get("step_name")
        if step_name == "channel_message":
            text = str((payload.get("input") or {}).get("message") or "")
            return self.run_channel_chat(
                tenant_id=tenant_id,
                agent_id=agent_id,
                text=text,
                session_id=str(payload.get("session_id") or ""),
                history=list(payload.get("_history") or []),
                skill_prompt=str(payload.get("_skill_prompt") or ""),
                source=source,
                correlation_id=correlation_id,
                delegation_confirmed=bool(payload.get("_delegation_confirmed")),
                pending_delegation=payload.get("_pending_delegation"),
            )

        payload = self._normalize_work_orders_dispatch_payload(payload)
        usage_scope = payload.get("usage_scope") if isinstance(payload.get("usage_scope"), dict) else {}
        provider = self._provider_override or build_provider(
            agent.model,
            scope={
                "tenant": tenant_id,
                "team": usage_scope.get("team_id"),
                "user": usage_scope.get("requested_by"),
                "workflow": payload.get("workflow_id"),
                "task": payload.get("task_id"),
                "agent": agent_id,
            },
        )
        system_prompt = self._build_workflow_system_prompt(agent, payload)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "workflow_id": payload.get("workflow_id"),
                        "task_id": payload.get("task_id"),
                        "step_name": payload.get("step_name"),
                        "input": payload.get("input") or {},
                        "required_artifacts": payload.get("required_artifacts") or [],
                        "skill_hints": canonical_skill_ids(payload.get("skill_hints")),
                    }
                ),
            },
        ]
        policy_ctx = ToolPolicyContext(
            tenant_id=tenant_id,
            agent_id=agent_id,
            source=source,
            is_subagent=is_subagent,
            parent_tools=parent_tools,
            channel_ingress=source in {"slack", "discord", "telegram"},
            model_id=agent.model,
            linked_workflow_id=str(payload.get("workflow_id") or "") or None,
            stage_tool_allow=_stage_tool_allow(payload),
            stage_external_effects=_stage_external_effects(payload),
            approval_resumable=(
                source == "workflow"
                and not is_subagent
                and bool(payload.get("workflow_id"))
                and bool(payload.get("task_id"))
            ),
        )
        session = get_session_store().get_or_create(
            tenant_id=tenant_id,
            source=source,
            agent_id=agent_id,
            workflow_id=payload.get("workflow_id"),
        )
        if policy_ctx.approval_resumable:
            self._replay_decided_approvals(agent, policy_ctx, payload, messages, correlation_id)
        return self._run_tool_loop(
            agent=agent,
            provider=provider,
            messages=messages,
            policy_ctx=policy_ctx,
            session=session,
            runtime_task_id=runtime_task_id,
            correlation_id=correlation_id,
            parse_payload=payload,
            parse_mode="workflow",
        )

    def _replay_decided_approvals(
        self,
        agent: AgentConfig,
        policy_ctx: ToolPolicyContext,
        payload: dict[str, Any],
        messages: list[dict[str, Any]],
        correlation_id: str | None,
    ) -> None:
        queue = get_approval_queue()
        workflow_id = str(payload.get("workflow_id") or "")
        task_id = str(payload.get("task_id") or "")
        for approval in queue.decided_for_task(workflow_id, task_id):
            args = dict(approval.args or {})
            if approval.status == "denied":
                content: dict[str, Any] = {
                    "decision": "denied",
                    "reason": f"rejected by {approval.decided_by_actor_id or 'a reviewer'}",
                    "tool_name": approval.tool_name,
                }
            else:
                ctx = ToolPolicyContext(**{**policy_ctx.__dict__, "approval_id": approval.approval_id})
                decision = evaluate_tool_policy(
                    approval.tool_name,
                    agent,
                    self.config,
                    ctx,
                    policy_engine=self.policy_engine,
                    opa_client=get_opa_client(),
                    tool_args=args,
                )
                get_tool_audit_log().record(
                    tenant_id=policy_ctx.tenant_id,
                    agent_id=policy_ctx.agent_id,
                    tool_name=approval.tool_name,
                    decision=decision.decision,
                    policy_id=decision.policy_id,
                    correlation_id=correlation_id,
                    args=args,
                )
                if decision.decision != "allow":
                    content = {
                        "error": decision.reason,
                        "decision": decision.decision,
                        "policy_id": decision.policy_id,
                    }
                else:
                    try:
                        result = self.registry.dispatch(
                            approval.tool_name,
                            {**args, "approval_id": approval.approval_id},
                            context=ctx,
                        )
                        content = {
                            "approved_call_executed": True,
                            "note": "Already executed after human approval. Do not call it again.",
                            "result": result,
                        }
                    except Exception as exc:  # noqa: BLE001
                        detail = redact_tool_error(str(exc), level=decision.redaction_level)
                        content = {"error": "tool execution failed", "detail": detail}
            queue.consume(approval.approval_id)
            call = {
                "id": f"approval-{approval.approval_id}",
                "type": "function",
                "function": {"name": approval.tool_name, "arguments": json.dumps(args)},
            }
            messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
            _append_tool_message(messages, call, json.dumps(content, default=str))

    def run_channel_chat(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        text: str,
        session_id: str,
        history: list[dict[str, Any]] | None = None,
        skill_prompt: str = "",
        source: str = "slack",
        correlation_id: str | None = None,
        delegation_confirmed: bool = False,
        pending_delegation: dict[str, Any] | None = None,
        workflow_confirmed: bool = False,
        pending_workflow: dict[str, Any] | None = None,
        program_confirmed: bool = False,
        pending_program: dict[str, Any] | None = None,
        program_advance_confirmed: bool = False,
        pending_program_advance: dict[str, Any] | None = None,
        workflow_linked: bool = False,
        linked_workflow_id: str | None = None,
        continue_delivery_confirmed: bool = False,
        bridge_confirmed: bool = False,
        pending_continue_delivery: dict[str, Any] | None = None,
        pending_bridge: dict[str, Any] | None = None,
        prd_phase: str | None = None,
        workflow_phase: str | None = None,
        channel_intent: str | None = None,
        informal_prd_opt_out: bool = False,
        session_mode: str | None = None,
        bridge_available: bool = False,
        system_context: str = "",
        actor_email: str | None = None,
        approval_id: str | None = None,
    ) -> AgentRunResult:
        runtime_task_id = str(uuid.uuid4())
        try:
            agent = self.config.get_agent(agent_id)
        except KeyError as exc:
            return AgentRunResult(status="blocked", error=str(exc), runtime_task_id=runtime_task_id)

        provider = self._provider_override or build_provider(
            agent.model, scope={"tenant": tenant_id, "user": actor_email, "agent": agent_id}
        )
        phase = workflow_phase or prd_phase
        system_prompt = self._build_channel_system_prompt(
            agent,
            skill_prompt,
            workflow_linked=workflow_linked,
            workflow_phase=phase,
            session_mode=session_mode,
            bridge_available=bridge_available,
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if history:
            from ts_harness.agent.transcript import repair_tool_history

            messages.extend(repair_tool_history(history))
        if system_context:
            messages.append({"role": "system", "content": system_context})
        messages.append({"role": "user", "content": text})

        policy_ctx = ToolPolicyContext(
            tenant_id=tenant_id,
            agent_id=agent_id,
            source=source,
            channel_ingress=True,
            model_id=agent.model,
            session_id=session_id or None,
            delegation_confirmed=delegation_confirmed,
            pending_delegation=pending_delegation,
            workflow_confirmed=workflow_confirmed,
            pending_workflow=pending_workflow,
            program_confirmed=program_confirmed,
            pending_program=pending_program,
            program_advance_confirmed=program_advance_confirmed,
            pending_program_advance=pending_program_advance,
            workflow_linked=workflow_linked,
            linked_workflow_id=linked_workflow_id,
            continue_delivery_confirmed=continue_delivery_confirmed,
            bridge_confirmed=bridge_confirmed or continue_delivery_confirmed,
            pending_continue_delivery=pending_continue_delivery or pending_bridge,
            pending_bridge=pending_bridge or pending_continue_delivery,
            prd_phase=phase,
            workflow_phase=phase,
            channel_intent=channel_intent,
            informal_prd_opt_out=informal_prd_opt_out,
            session_mode=session_mode,
            bridge_available=bridge_available,
            actor_email=actor_email,
            approval_id=approval_id,
        )
        session = get_session_store().get(session_id) if session_id else None
        if session is None:
            session = get_session_store().get_or_create(
                tenant_id=tenant_id,
                source=source,
                agent_id=agent_id,
                channel=source,
            )
        return self._run_tool_loop(
            agent=agent,
            provider=provider,
            messages=messages,
            policy_ctx=policy_ctx,
            session=session,
            runtime_task_id=runtime_task_id,
            correlation_id=correlation_id,
            parse_payload={"step_name": "channel_message"},
            parse_mode="channel",
        )

    def _run_tool_loop(
        self,
        *,
        agent: AgentConfig,
        provider: ModelProvider,
        messages: list[dict[str, Any]],
        policy_ctx: ToolPolicyContext,
        session: SessionRecord,
        runtime_task_id: str,
        correlation_id: str | None,
        parse_payload: dict[str, Any],
        parse_mode: str,
    ) -> AgentRunResult:
        tool_defs = self.registry.definitions_for_agent(
            agent,
            policy_ctx,
            self.config,
        )
        if parse_mode == "workflow":
            tool_defs = [
                tool
                for tool in tool_defs
                if tool.get("function", {}).get("name") != "workflow_update"
            ]
            # Work-order stages must use the dispatch payload, not memory recall
            # preambles that get pasted into artifact keys.
            if self._requires_work_orders_output(parse_payload):
                tool_defs = [
                    tool
                    for tool in tool_defs
                    if tool.get("function", {}).get("name") not in _WORKFLOW_MEMORY_TOOLS
                ]
        tool_audit: list[dict[str, Any]] = []
        output_repairs = 0
        channel_summary_repairs = 0
        awaiting_confirmation = False
        max_turns = parse_payload.get("max_turns")
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            max_turns = self.max_iterations

        for _ in range(max_turns):
            if self._interrupt_reason:
                reason = self._interrupt_reason
                self._interrupt_reason = None
                session.status = "interrupted"
                get_session_store().save(session)
                return AgentRunResult(
                    status="blocked",
                    error=f"interrupted: {reason}",
                    runtime_task_id=runtime_task_id,
                    messages=messages,
                    tool_calls=tool_audit,
                )
            if self._paused:
                session.status = "paused"
                get_session_store().save(session)
                return AgentRunResult(
                    status="blocked",
                    error="paused",
                    runtime_task_id=runtime_task_id,
                    messages=messages,
                    tool_calls=tool_audit,
                )
            response = provider.chat(messages, tools=tool_defs or None)
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            if self.on_stream_chunk and message.get("content"):
                self.on_stream_chunk(str(message["content"]))
            messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content") or ""
                if parse_mode == "channel":
                    output = self._parse_channel_output(content)
                    if not str(output.get("summary") or "").strip():
                        if channel_summary_repairs < 1:
                            channel_summary_repairs += 1
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "Reply in plain language. Include a non-empty "
                                        "summary of what you just did."
                                    ),
                                }
                            )
                            continue
                        output = {
                            **output,
                            "summary": (
                                "Done. I completed that step but did not write a summary."
                            ),
                        }
                    if awaiting_confirmation:
                        output = {**output, "summary": with_confirmation_prompt(str(output.get("summary") or ""))}
                else:
                    output = self._parse_output(content, parse_payload)
                    contract = self._contract_for_payload(parse_payload)
                    outcome = "succeeded"
                    if contract is not None:
                        violations = stage_contracts.validate_stage_output(
                            contract,
                            output,
                            parse_payload.get("input") or {},
                            tool_audit,
                        )
                        if violations:
                            validation_error = "; ".join(
                                f"{v.path or '/'}: {v.message}" for v in violations
                            )
                        else:
                            validation_error = None
                            outcome = stage_contracts.resolve_stage_outcome(
                                contract, output
                            )
                            if "validate_coding_backend_pr_url" in (contract.get("semantic_validators") or []):
                                output = {**output, **stage_contracts.trusted_coding_backend_fields(tool_audit)}
                    elif self._legacy_stage_validators_enabled():
                        validation_error = self._validate_workflow_output(
                            output,
                            parse_payload,
                            tool_calls=tool_audit,
                        )
                    else:
                        validation_error = None
                    if validation_error:
                        if (
                            parse_mode == "workflow"
                            and output_repairs < _MAX_WORKFLOW_OUTPUT_REPAIRS
                        ):
                            output_repairs += 1
                            messages.append(
                                {
                                    "role": "user",
                                    "content": self._output_repair_note(
                                        parse_payload, validation_error, contract
                                    ),
                                }
                            )
                            continue
                        session.status = "blocked"
                        get_session_store().save(session)
                        output = self._redact_summary(output)
                        return AgentRunResult(
                            status="blocked",
                            output=output,
                            error=validation_error,
                            runtime_task_id=runtime_task_id,
                            messages=messages,
                            tool_calls=tool_audit,
                        )
                    if contract is not None and outcome == "blocked":
                        # A structurally-valid output whose declared outcome maps to
                        # blocked (e.g. a QA fail). Do not repair and do not convert
                        # to succeeded; surface the blocked status to the runner.
                        session.status = "blocked"
                        get_session_store().save(session)
                        output = self._redact_summary(output)
                        return AgentRunResult(
                            status="blocked",
                            output=output,
                            error="stage contract outcome: blocked",
                            runtime_task_id=runtime_task_id,
                            messages=messages,
                            tool_calls=tool_audit,
                        )
                session.status = "completed"
                get_session_store().save(session)
                if self.on_audit:
                    self.on_audit(
                        {
                            "event": "agent_run_completed",
                            "tenant_id": policy_ctx.tenant_id,
                            "agent_id": policy_ctx.agent_id,
                            "correlation_id": correlation_id,
                            "runtime_task_id": runtime_task_id,
                            "tool_calls": tool_audit,
                        }
                    )
                output = self._redact_summary(output)
                return AgentRunResult(
                    status="succeeded",
                    output=output,
                    runtime_task_id=runtime_task_id,
                    proposed_tasks=output.get("proposed_tasks"),
                    messages=messages,
                    tool_calls=tool_audit,
                )

            for call in tool_calls:
                fn = call.get("function") or {}
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}
                tool_ctx = policy_ctx
                arg_approval_id = str(args.get("approval_id") or "").strip()
                if arg_approval_id:
                    tool_ctx = ToolPolicyContext(
                        **{**policy_ctx.__dict__, "approval_id": arg_approval_id}
                    )
                dispatch_args = {k: v for k, v in args.items() if k != "approval_id"}
                if tool_name == "run_coding_backend":
                    dispatch_args = self._enrich_coding_backend_args(
                        dispatch_args, parse_payload
                    )
                decision = evaluate_tool_policy(
                    tool_name,
                    agent,
                    self.config,
                    tool_ctx,
                    policy_engine=self.policy_engine,
                    opa_client=get_opa_client(),
                    tool_args=dispatch_args,
                )
                get_tool_audit_log().record(
                    tenant_id=policy_ctx.tenant_id,
                    agent_id=policy_ctx.agent_id,
                    tool_name=tool_name,
                    decision=decision.decision,
                    policy_id=decision.policy_id,
                    correlation_id=correlation_id,
                    args=dispatch_args,
                )
                result: Any = None
                if decision.decision == "approval_required":
                    resumable = parse_mode == "workflow" and policy_ctx.approval_resumable
                    approval = get_approval_queue().request(
                        tenant_id=policy_ctx.tenant_id,
                        agent_id=policy_ctx.agent_id,
                        tool_name=tool_name,
                        args=dispatch_args,
                        reason=decision.reason,
                        workflow_id=policy_ctx.linked_workflow_id if resumable else None,
                        task_id=(str(parse_payload.get("task_id") or "") or None) if resumable else None,
                    )
                    session.metadata = dict(session.metadata or {})
                    session.metadata["pending_tool_approval"] = {
                        "approval_id": approval.approval_id,
                        "tool_name": tool_name,
                        "args": dispatch_args,
                    }
                    get_session_store().save(session)
                    result_text = json.dumps(
                        {
                            "decision": "approval_required",
                            "approval_id": approval.approval_id,
                            "policy_id": decision.policy_id,
                            "reason": decision.reason,
                            "preview": dispatch_args,
                        }
                    )
                    if resumable:
                        tool_audit.append(
                            {"tool": tool_name, "args": args, "decision": decision.decision}
                        )
                        _close_tool_calls(messages, tool_calls, call, result_text)
                        session.status = "blocked"
                        get_session_store().save(session)
                        return AgentRunResult(
                            status="blocked",
                            error=f"{AWAITING_TOOL_APPROVAL_PREFIX}{approval.approval_id}",
                            output={
                                "approval_id": approval.approval_id,
                                "tool_name": tool_name,
                                "preview": dispatch_args,
                            },
                            runtime_task_id=runtime_task_id,
                            messages=messages,
                            tool_calls=tool_audit,
                        )
                elif decision.decision != "allow":
                    result_text = json.dumps(
                        {
                            "error": decision.reason,
                            "decision": decision.decision,
                            "policy_id": decision.policy_id,
                        }
                    )
                else:
                    try:
                        result = self.registry.dispatch(tool_name, dispatch_args, context=tool_ctx)
                        result_text = json.dumps(result)
                        if arg_approval_id and decision.policy_id == "approval_consumed":
                            get_approval_queue().consume(arg_approval_id)
                            session.metadata = dict(session.metadata or {})
                            session.metadata.pop("pending_tool_approval", None)
                            get_session_store().save(session)
                        if (
                            policy_ctx.channel_ingress
                            and isinstance(result, dict)
                            and result.get("status") == "awaiting_confirmation"
                        ):
                            awaiting_confirmation = True
                            formatter = _proposal_formatter(tool_name)
                            if formatter is not None:
                                session.status = "completed"
                                get_session_store().save(session)
                                _close_tool_calls(messages, tool_calls, call, result_text)
                                return AgentRunResult(
                                    status="succeeded",
                                    output={
                                        "summary": formatter(result),
                                        **result,
                                    },
                                    runtime_task_id=runtime_task_id,
                                    messages=messages,
                                    tool_calls=tool_audit,
                                )
                        if tool_name == "delegate_task" and policy_ctx.channel_ingress:
                            from ts_harness.gateway.delegate_confirmation import clear_pending_delegate

                            clear_pending_delegate(session)
                    except Exception as exc:  # noqa: BLE001
                        detail = redact_tool_error(str(exc), level=decision.redaction_level)
                        result_text = json.dumps({"error": "tool execution failed", "detail": detail})
                record = {
                    "tool": tool_name,
                    "args": args,
                    "decision": decision.decision,
                    # Keep audit preview small but parseable. Full coding results
                    # include log_tail and blow past 500 chars, which previously
                    # broke deliver_work_order validation (truncated JSON).
                    "result_preview": self._tool_audit_preview(
                        tool_name, result, result_text
                    ),
                }
                if tool_name == "run_coding_backend" and isinstance(result, dict):
                    record["coding_result"] = {
                        key: result.get(key)
                        for key in ("ok", *stage_contracts.TRUSTED_CODING_BACKEND_FIELDS)
                    }
                tool_audit.append(record)
                session.tool_count += 1
                get_session_store().save(session)
                if self.on_tool_call:
                    self.on_tool_call(record)
                if self._interrupt_reason:
                    reason = self._interrupt_reason
                    self._interrupt_reason = None
                    session.status = "interrupted"
                    get_session_store().save(session)
                    _close_tool_calls(messages, tool_calls, call, result_text)
                    return AgentRunResult(
                        status="blocked",
                        error=f"interrupted: {reason}",
                        runtime_task_id=runtime_task_id,
                        messages=messages,
                        tool_calls=tool_audit,
                    )
                _append_tool_message(messages, call, result_text)

        session.status = "blocked"
        get_session_store().save(session)
        return AgentRunResult(
            status="blocked",
            error="max iterations exceeded",
            runtime_task_id=runtime_task_id,
            messages=messages,
            tool_calls=tool_audit,
        )

    def _redact_summary(self, output: dict[str, Any]) -> dict[str, Any]:
        summary = output.get("summary")
        if not isinstance(summary, str) or not summary:
            return output
        from ts_harness.secrets.resolver import get_secret_resolver

        redacted = get_secret_resolver().redact(summary)
        if redacted == summary:
            return output
        return {**output, "summary": redacted}

    def _build_workflow_system_prompt(self, agent: AgentConfig, payload: dict[str, Any]) -> str:
        skill_hints = canonical_skill_ids(payload.get("skill_hints"))
        required_artifacts = list(payload.get("required_artifacts") or [])
        hints = ", ".join(skill_hints) or "none"
        artifacts = ", ".join(required_artifacts) or "none"
        extra = ""
        contract = self._contract_for_payload(payload)
        if contract is not None:
            # Declarative contract supersedes skill-specific format branches.
            prompt = (
                f"You are {agent.id}, an enterprise specialist agent.\n"
                f"{_soul_paragraph(agent)}"
                f"Workspace: {agent.workspace}\n"
                f"Required artifacts: {artifacts}\n"
                f"Skill hints: {hints}\n"
                f"{payload.get('_skill_prompt', '')}\n"
                "Complete the assigned workflow step. Return JSON with keys "
                "summary and artifacts when done.\n"
                f"\n{stage_contracts.summarize_stage_contract(contract)}\n"
            )
            task_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
            qa_feedback = str(task_input.get("qa_feedback") or "").strip()
            if qa_feedback:
                prompt += (
                    "\nQA revision mode:\n"
                    "- Call run_coding_backend with input.work_order_id, "
                    "input.work_order_body, input.qa_feedback, and input.pr_url "
                    "when present.\n"
                    "- Address every QA defect. Update the existing PR; do not "
                    "open a new one.\n"
                    f"- QA feedback:\n{qa_feedback}\n"
                )
            return prompt
        if (
            "clarify-requirements" in skill_hints
            or "clarified_requirements" in required_artifacts
            or payload.get("step_name") == "intake"
        ):
            extra = (
                "\nClarifying Intake mode:\n"
                "- Ask concise clarifying questions needed to draft the PRD.\n"
                "- Do not draft, outline, or review the PRD in this step.\n"
                "- Prefer 3-7 questions covering audience, scope, must-have "
                "features, constraints, success metrics, and launch timing.\n"
                "- Return JSON with summary, questions, confidence, warnings, "
                "and follow_up_needed.\n"
                "- Put each question as a string in the top-level questions "
                "array; do not put the PRD body in summary or artifacts.\n"
            )
        elif (
            "prd-to-work-orders" in skill_hints
            and "prd-to-work-orders-governed" not in skill_hints
        ):
            extra = (
                "\nLegacy work-orders hint: prefer prd-to-work-orders-governed "
                "for governed dispatch. Use input.approved_prd and input.stage; "
                "return JSON artifacts per runtime-contract.\n"
            )
        elif "prd-to-work-orders-governed" in skill_hints:
            task_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
            stage = str(task_input.get("stage") or "").strip().lower()
            if not stage:
                step_name = str(payload.get("step_name") or "")
                stage = {
                    "refine_requirements": "refinery",
                    "design_blueprints": "foundry",
                    "plan_work_orders": "planner",
                }.get(step_name, "")
            expected_keys = ", ".join(
                WORK_ORDERS_STAGE_ARTIFACT_KEYS.get(stage, ())
            ) or "stage *_markdown keys"
            extra = (
                "\nGoverned work-orders stage mode:\n"
                f"- Execute exactly one stage: {stage or 'from input.stage'}.\n"
                "- Use input.approved_prd and optional prior-stage artifacts as source truth.\n"
                "- Carry every technical constraint the PRD states (frameworks, "
                "storage, named entities, intervals, limits) into this stage's "
                "artifacts word for word; never drop or generalize one.\n"
                "- Do not call recall_memory / remember_memory; the payload is complete.\n"
                "- Do not summarize or restate the PRD; produce new stage artifacts.\n"
                f"- artifacts must include distinct bodies for: {expected_keys}.\n"
                "- Return JSON with summary, artifacts (*_markdown keys), confidence, "
                "warnings, and follow_up_needed.\n"
                "- Never return an action plan, memory-recall preamble, or numbered "
                "next steps as the primary output.\n"
            )
        elif (
            "verified-merge-governed" in skill_hints
            or payload.get("step_name") == "deliver_work_order"
        ):
            extra = (
                "\nGoverned deliver_work_order mode:\n"
                "- Call run_coding_backend with input.work_order_id and input.work_order_body.\n"
                "- When present, also pass input.refinery_artifacts, input.foundry_artifacts, "
                "and input.planner_artifacts so the host can materialize docs/ for OpenCode.\n"
                "- Do not use sandbox exec for OpenCode, git, or gh.\n"
                "- Return JSON with work_order_id and pr_url from the tool result.\n"
                "- Fail the stage if work_order_body or pr_url is missing.\n"
            )
        elif "draft-prd" in skill_hints or "prd_draft" in required_artifacts:
            extra = (
                "\nDraft / Revise PRD mode:\n"
                "- Use input.clarified_requirements and input.intake_output "
                "as the source of truth.\n"
                "- input.clarified_requirements.initial_request is the requester's "
                "own request and clarified_requirements.answers are their answers. "
                "Every requirement, named item, number, and technology in them "
                "must appear in the PRD; answers refine the request and win on "
                "conflict.\n"
                "- Keep stated specifics exact (frameworks, named assets or "
                "entities, intervals, limits, storage, validation rules). Never "
                "replace them with generic wording.\n"
                "- Add a 'Requirements' section listing each stated requirement as "
                "a checklist item, and a 'Technical Constraints' section listing "
                "every technology, platform, storage, and data choice the "
                "requester stated.\n"
                "- Do not ask for more intake information unless a critical "
                "blocker is explicitly marked unknown.\n"
                "- Produce a complete draft PRD in Markdown.\n"
                "- Return JSON with result.prd_title, result.prd_status='Draft', "
                "result.prd_markdown, confidence, warnings, and follow_up_needed.\n"
                "- Put the full PRD only in result.prd_markdown; do not add preamble, "
                "next-step instructions, or markdown code fences outside the JSON.\n"
            )
        return (
            f"You are {agent.id}, an enterprise specialist agent.\n"
            f"{_soul_paragraph(agent)}"
            f"Workspace: {agent.workspace}\n"
            f"Required artifacts: {artifacts}\n"
            f"Skill hints: {hints}\n"
            f"{payload.get('_skill_prompt', '')}\n"
            "Complete the assigned workflow step. Return JSON with keys "
            "summary and artifacts when done.\n"
            f"{extra}"
        )

    def _build_channel_system_prompt(
        self,
        agent: AgentConfig,
        skill_prompt: str,
        *,
        workflow_linked: bool = False,
        workflow_phase: str | None = None,
        session_mode: str | None = None,
        bridge_available: bool = False,
    ) -> str:
        if session_mode == "program_builder":
            return (
                f"You are {agent.id}, the program builder.\n"
                f"{_soul_paragraph(agent)}"
                f"{skill_prompt}\n"
                "This is the admin console program builder. The person describes a program; "
                "you draft its workflow, program, and routing intent with builder_catalog and "
                "propose_program_bundle, and propose missing skills with propose_skill.\n"
                "You only write drafts. Never say a definition is active or live; the person "
                "activates the bundle from the builder page.\n"
                "Reply in natural language."
            )
        linked_rules = ""
        linked_modes = {"workflow_linked", "prd_linked", "bridge_linked", "delivery_linked"}
        if session_mode in linked_modes or workflow_linked:
            linked_rules = (
                "\nThis thread is linked to a durable workflow. Use workflow_status for progress, "
                "workflow_approve or workflow_reject_with_feedback for human gates. "
                "Do not call start_workflow or workflow_update from channel chat.\n"
            )
            if workflow_phase:
                linked_rules += f"Current workflow phase: {workflow_phase}.\n"
            if workflow_phase == "awaiting_approval":
                linked_rules += (
                    "A PRD draft is awaiting approval. Do not regenerate or rewrite the PRD "
                    "in channel chat. Tell the user to reply yes to approve, or no with feedback "
                    "to trigger a governed revision.\n"
                )
            if bridge_available:
                linked_rules += (
                    "The source workflow is approved and a downstream bridge is configured. "
                    "When the user agrees, call propose_bridge_workflow then bridge_workflow after confirmation.\n"
                )
        return (
            f"You are {agent.id}, the enterprise orchestrator.\n"
            f"{_soul_paragraph(agent)}"
            f"Workspace: {agent.workspace}\n"
            f"{skill_prompt}\n"
            "This is a conversational channel message (Slack/DM), not a workflow task.\n"
            "Apply Step 0 from route-task first. If Step 0 passes, respond directly.\n"
            "When specialist work is required, call propose_delegate_task first and ask the user "
            "to confirm. Do not call delegate_task until the user explicitly confirmed on the next turn.\n"
            "When a governed workflow is required, call propose_start_workflow with the configured workflow_type.\n"
            "Do not call workflow_update from channel chat.\n"
            f"{linked_rules}"
            "When the user asks about a program's status, call program_status and answer "
            "from that result rather than an earlier tool snapshot.\n"
            "Reply in natural language. Plain text is fine; if you return JSON, include a summary field."
        )

    def _build_system_prompt(self, agent: AgentConfig, payload: dict[str, Any]) -> str:
        if payload.get("step_name") == "channel_message":
            return self._build_channel_system_prompt(agent, str(payload.get("_skill_prompt") or ""))
        return self._build_workflow_system_prompt(agent, payload)

    def _parse_channel_output(self, content: str) -> dict[str, Any]:
        text = (content or "").strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    if "summary" not in parsed and parsed.get("content"):
                        parsed["summary"] = parsed["content"]
                    if "summary" not in parsed:
                        parsed["summary"] = text
                    return parsed
            except json.JSONDecodeError:
                pass
        return {"summary": text, "step_name": "channel_message"}

    def _parse_output(self, content: str, payload: dict[str, Any]) -> dict[str, Any]:
        text = (content or "").strip()
        parsed = self._parse_json_object(text)
        if parsed is not None:
            return self._normalize_work_orders_output(
                self._sanitize_prd_output(parsed, payload),
                payload,
                raw_text=text,
            )
        output = {
            "summary": text,
            "workflow_id": payload.get("workflow_id"),
            "task_id": payload.get("task_id"),
            "step_name": payload.get("step_name"),
        }
        if self._requires_intake_output(payload):
            questions = self._extract_intake_questions(text)
            if questions and not self._looks_like_prd_markdown(text):
                output["questions"] = questions
                output["confidence"] = "medium"
                output["warnings"] = [
                    "Normalized plain-text intake questions without JSON "
                    "wrapper."
                ]
                output["follow_up_needed"] = True
        if self._requires_prd_output(payload) and self._looks_like_prd_markdown(
            text
        ):
            sanitized = self._sanitize_prd_markdown(text)
            output["result"] = {
                "prd_title": self._prd_title_from_markdown(sanitized),
                "prd_status": "Draft",
                "prd_markdown": sanitized,
            }
            output["confidence"] = "medium"
            output["warnings"] = [
                "Normalized Markdown PRD response without JSON wrapper."
            ]
            output["follow_up_needed"] = False
        return self._normalize_work_orders_output(output, payload, raw_text=text)

    def _parse_json_object(self, text: str) -> dict[str, Any] | None:
        candidates = [text]
        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            while lines and lines[-1].strip() == "```":
                lines.pop()
            candidates.append("\n".join(lines).strip())
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start: end + 1])
        for candidate in candidates:
            if not candidate.startswith("{"):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _sanitize_prd_markdown(self, text: str) -> str:
        from ts_harness.workflows.artifacts import sanitize_prd_markdown

        return sanitize_prd_markdown(text)

    def _sanitize_prd_output(
        self,
        output: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._requires_prd_output(payload):
            return output
        result = output.get("result")
        if isinstance(result, dict):
            markdown = result.get("prd_markdown")
            if isinstance(markdown, str) and markdown.strip():
                updated = dict(result)
                updated["prd_markdown"] = self._sanitize_prd_markdown(markdown)
                output = dict(output)
                output["result"] = updated
        markdown = output.get("prd_markdown")
        if isinstance(markdown, str) and markdown.strip():
            output = dict(output)
            output["prd_markdown"] = self._sanitize_prd_markdown(markdown)
        artifacts = output.get("artifacts")
        if isinstance(artifacts, dict):
            artifact_markdown = (
                artifacts.get("prd_markdown") or artifacts.get("markdown")
            )
            if isinstance(artifact_markdown, str) and artifact_markdown.strip():
                output = dict(output)
                updated = dict(result) if isinstance(result, dict) else {}
                artifact_title = artifacts.get("prd_title") or artifacts.get("title")
                if artifact_title and "prd_title" not in updated:
                    updated["prd_title"] = artifact_title
                updated["prd_status"] = str(updated.get("prd_status") or "Draft")
                updated["prd_markdown"] = self._sanitize_prd_markdown(
                    artifact_markdown
                )
                output["result"] = updated
        return _finalize_prd_output(output, payload)

    def _requires_prd_output(self, payload: dict[str, Any]) -> bool:
        required_artifacts = {
            str(item) for item in payload.get("required_artifacts") or []
        }
        skill_hints = set(canonical_skill_ids(payload.get("skill_hints")))
        step_name = str(payload.get("step_name") or "")
        return (
            "prd_draft" in required_artifacts
            or "draft-prd" in skill_hints
            or step_name in {"draft_prd", "revise_prd"}
        )

    def _requires_intake_output(self, payload: dict[str, Any]) -> bool:
        required_artifacts = {
            str(item) for item in payload.get("required_artifacts") or []
        }
        skill_hints = set(canonical_skill_ids(payload.get("skill_hints")))
        step_name = str(payload.get("step_name") or "")
        return (
            "clarified_requirements" in required_artifacts
            or "clarify-requirements" in skill_hints
            or step_name == "intake"
        )

    def _requires_work_orders_output(self, payload: dict[str, Any]) -> bool:
        skill_hints = set(canonical_skill_ids(payload.get("skill_hints")))
        return (
            "prd-to-work-orders-governed" in skill_hints
            or "prd-to-work-orders" in skill_hints
        )

    def _requires_prepare_handoff_output(self, payload: dict[str, Any]) -> bool:
        skill_hints = set(canonical_skill_ids(payload.get("skill_hints")))
        step_name = str(payload.get("step_name") or "")
        return (
            "source-work-order-governed" in skill_hints
            or step_name == "prepare_work_order_delivery"
        )

    def _requires_deliver_work_order_output(self, payload: dict[str, Any]) -> bool:
        skill_hints = set(canonical_skill_ids(payload.get("skill_hints")))
        step_name = str(payload.get("step_name") or "")
        return (
            "verified-merge-governed" in skill_hints
            or "verified-merge" in skill_hints
            or step_name == "deliver_work_order"
        )

    def _has_deliver_work_order_output(self, output: dict[str, Any]) -> bool:
        work_order_id = output.get("work_order_id")
        pr_url = output.get("pr_url")
        return (
            isinstance(work_order_id, str)
            and bool(work_order_id.strip())
            and isinstance(pr_url, str)
            and pr_url.strip().startswith("http")
        )

    @staticmethod
    def _tool_audit_preview(tool_name: str, result: Any, result_text: str) -> str:
        """Build a short audit preview that stays valid JSON for key tools."""
        if tool_name == "run_coding_backend" and isinstance(result, dict):
            compact = {
                "ok": result.get("ok"),
                "pr_url": result.get("pr_url"),
                "error": result.get("error"),
                "branch": result.get("branch"),
                "work_order_id": result.get("work_order_id"),
            }
            return json.dumps(compact, default=str)[:500]
        if tool_name == "propose_skill" and isinstance(result, dict):
            def _short(value: Any, limit: int = 160) -> Any:
                if isinstance(value, str) and len(value) > limit:
                    return value[: limit - 3] + "..."
                return value

            compact = {
                "status": result.get("status"),
                "skill_id": result.get("skill_id"),
                "revision_of": result.get("revision_of"),
                "path": _short(result.get("path")),
                "error": _short(result.get("error")),
                "error_code": result.get("error_code"),
            }
            return json.dumps(compact, default=str)[:500]
        return (result_text or "")[:500]

    def _deliver_work_order_tool_failure_reason(
        self,
        output: dict[str, Any],
        tool_calls: list[dict[str, Any]] | None,
    ) -> str | None:
        """Require pr_url to come from a successful run_coding_backend result."""
        if not self._has_deliver_work_order_output(output):
            return (
                "deliver_work_order output must include work_order_id and "
                "pr_url (https URL from run_coding_backend)"
            )
        claimed_pr = str(output.get("pr_url") or "").strip()
        last_coding: dict[str, Any] | None = None
        for record in reversed(tool_calls or []):
            if record.get("tool") != "run_coding_backend":
                continue
            if record.get("decision") != "allow":
                continue
            preview = record.get("result_preview")
            parsed: dict[str, Any] | None = None
            if isinstance(preview, dict):
                parsed = preview
            elif isinstance(preview, str) and preview.strip():
                try:
                    candidate = json.loads(preview)
                except json.JSONDecodeError:
                    candidate = None
                if isinstance(candidate, dict):
                    parsed = candidate
            if parsed is not None:
                last_coding = parsed
                break
        if last_coding is None:
            return (
                "deliver_work_order requires a successful run_coding_backend "
                "tool call that returns pr_url"
            )
        if last_coding.get("ok") is not True:
            err = str(last_coding.get("error") or "run_coding_backend failed").strip()
            return f"deliver_work_order cannot succeed when run_coding_backend failed: {err}"
        tool_pr = str(last_coding.get("pr_url") or "").strip()
        if not tool_pr.startswith("http"):
            return (
                "deliver_work_order requires run_coding_backend to return a "
                "https pr_url"
            )
        if tool_pr != claimed_pr:
            return (
                "deliver_work_order pr_url must match run_coding_backend result "
                f"(output={claimed_pr}, tool={tool_pr})"
            )
        return None

    def _has_prepare_handoff_output(self, output: dict[str, Any]) -> bool:
        if output.get("planner_complete") is not True:
            return False
        if output.get("work_orders_handoff") is not True:
            return False
        next_id = output.get("next_work_order_id")
        if not isinstance(next_id, str) or not next_id.strip():
            return False
        work_order_ids = output.get("work_order_ids")
        return (
            isinstance(work_order_ids, list)
            and any(isinstance(item, str) and item.strip() for item in work_order_ids)
        )

    def _work_orders_stage(self, payload: dict[str, Any]) -> str:
        input_data = payload.get("input")
        if isinstance(input_data, dict):
            stage = str(input_data.get("stage") or "").strip().lower()
            if stage:
                return stage
        step_name = str(payload.get("step_name") or "")
        if step_name == "refine_requirements":
            return "refinery"
        if step_name == "design_blueprints":
            return "foundry"
        if step_name == "plan_work_orders":
            return "planner"
        return ""

    def _normalize_work_orders_dispatch_payload(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Prefer plain PRD markdown for work-order stages instead of task envelopes."""
        if not self._requires_work_orders_output(payload):
            return payload
        input_data = payload.get("input")
        if not isinstance(input_data, dict):
            return payload
        approved = input_data.get("approved_prd")
        if not isinstance(approved, dict):
            return payload
        markdown = artifact_body(approved)
        if not markdown.strip():
            return payload
        normalized_input = dict(input_data)
        normalized_input["approved_prd"] = markdown
        result = approved.get("result") if isinstance(approved.get("result"), dict) else {}
        title = (
            approved.get("prd_title")
            or result.get("prd_title")
            or approved.get("title")
        )
        if title:
            normalized_input["approved_prd_title"] = title
        return {**payload, "input": normalized_input}

    def _has_work_orders_artifacts(
        self,
        output: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        artifacts = output.get("artifacts")
        stage = self._work_orders_stage(payload)
        return work_orders_artifacts_failure_reason(artifacts, stage) is None

    def _work_orders_artifact_keys(self, stage: str) -> set[str]:
        return set(WORK_ORDERS_STAGE_ARTIFACT_KEYS.get(stage, ()))

    def _extract_work_orders_artifacts_from_markdown(
        self,
        text: str,
        stage: str,
    ) -> dict[str, str]:
        if len(text.strip()) < 200:
            return {}
        if looks_like_memory_recall_summary(text):
            return {}
        lowered = text.lower()
        if self._looks_like_prd_recap(text, stage):
            return {}
        if stage == "foundry" and looks_like_invalid_foundry_content(text):
            return {}
        artifacts: dict[str, str] = {}
        if stage == "refinery":
            partitioned = partition_refinery_markdown(text)
            if len(partitioned) > 1:
                return partitioned
            if "feature tree" in lowered or "# feature" in lowered:
                artifacts["feature_tree_markdown"] = text.strip()
            if "frd" in lowered or "feature requirements" in lowered:
                artifacts["frds_markdown"] = text.strip()
            if (
                "product overview" in lowered
                or "business problem" in lowered
                or "personas" in lowered
            ):
                artifacts["product_overview_markdown"] = text.strip()
            if not artifacts and (
                "requirement" in lowered
                or "req-" in lowered
                or "when " in lowered
            ):
                artifacts["requirements_markdown"] = text.strip()
        elif stage == "foundry":
            partitioned = partition_foundry_markdown(text)
            if partitioned:
                return partitioned
            if re.search(r"\badr[-\s]?\d", lowered, re.IGNORECASE) or (
                "architecture decision" in lowered and has_foundry_structural_markers(text)
            ):
                artifacts["architecture_decisions_markdown"] = text.strip()
            if re.search(r"\b(?:fb|cb|comp)[-\s]?\d", lowered, re.IGNORECASE) or re.search(
                r"^#{1,6}\s*(?:component |feature )?blueprint",
                text,
                re.IGNORECASE | re.MULTILINE,
            ):
                artifacts["blueprints_markdown"] = text.strip()
            if re.search(
                r"^#{1,6}\s*(?:system design|architecture overview|architecture summary|deployment architecture)",
                text,
                re.IGNORECASE | re.MULTILINE,
            ):
                artifacts["architecture_summary_markdown"] = text.strip()
            if not artifacts and has_foundry_structural_markers(text):
                artifacts["blueprints_markdown"] = text.strip()
        elif stage == "planner":
            if "work order" in lowered or "wo-" in lowered:
                artifacts["work_orders_markdown"] = text.strip()
            if "context graph" in lowered:
                artifacts["context_graph_markdown"] = text.strip()
            if "execution phase" in lowered:
                artifacts["execution_phases_markdown"] = text.strip()
        return artifacts

    def _looks_like_prd_recap(self, text: str, stage: str) -> bool:
        return looks_like_prd_recap_text(text, stage)

    def _normalize_work_orders_output(
        self,
        output: dict[str, Any],
        payload: dict[str, Any],
        *,
        raw_text: str = "",
    ) -> dict[str, Any]:
        if not self._requires_work_orders_output(payload):
            return output
        if self._has_work_orders_artifacts(output, payload):
            return output

        stage = self._work_orders_stage(payload)
        expected_keys = self._work_orders_artifact_keys(stage)
        artifacts: dict[str, str] = {}
        existing = output.get("artifacts")
        if isinstance(existing, dict):
            for key, value in existing.items():
                if (
                    isinstance(value, str)
                    and value.strip()
                    and not looks_like_memory_recall_summary(value)
                ):
                    artifacts[key] = value.strip()

        def _accept_artifact_value(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            cleaned = value.strip()
            if not cleaned or looks_like_memory_recall_summary(cleaned):
                return None
            return cleaned

        for container_key in ("result",):
            container = output.get(container_key)
            if not isinstance(container, dict):
                continue
            nested = container.get("artifacts")
            if isinstance(nested, dict):
                for key, value in nested.items():
                    accepted = _accept_artifact_value(value)
                    if accepted:
                        artifacts[key] = accepted
            for key in expected_keys:
                accepted = _accept_artifact_value(container.get(key))
                if accepted:
                    artifacts[key] = accepted

        for key in expected_keys:
            accepted = _accept_artifact_value(output.get(key))
            if accepted:
                artifacts[key] = accepted

        text_candidates = [
            raw_text,
            output.get("summary"),
            output.get("content"),
            output.get("markdown"),
        ]
        for candidate in text_candidates:
            if not isinstance(candidate, str):
                continue
            extracted = self._extract_work_orders_artifacts_from_markdown(
                candidate,
                stage,
            )
            for key, value in extracted.items():
                artifacts.setdefault(key, value)
            if stage == "foundry" and candidate.strip():
                partitioned = partition_foundry_markdown(candidate)
                for key, value in partitioned.items():
                    artifacts.setdefault(key, value)
            if self._work_orders_artifact_keys(stage).intersection(artifacts):
                break

        if stage == "foundry":
            expected = self._work_orders_artifact_keys(stage)
            missing = expected - set(artifacts)
            if missing:
                source = next(
                    (
                        str(candidate).strip()
                        for candidate in text_candidates
                        if isinstance(candidate, str) and candidate.strip()
                    ),
                    "",
                )
                if source:
                    partitioned = partition_foundry_markdown(source)
                    for key in missing:
                        value = partitioned.get(key)
                        if isinstance(value, str) and value.strip():
                            artifacts[key] = value.strip()

        if not artifacts:
            return output

        normalized = dict(output)
        normalized["artifacts"] = artifacts
        normalized.setdefault("confidence", "medium")
        warnings = list(normalized.get("warnings") or [])
        warnings.append(
            "Normalized work-order stage artifacts without JSON wrapper."
        )
        normalized["warnings"] = warnings
        return normalized

    def _has_intake_questions(self, output: dict[str, Any]) -> bool:
        candidates: list[Any] = [
            output.get("questions"),
            output.get("clarifying_questions"),
        ]
        artifacts = output.get("artifacts")
        if isinstance(artifacts, dict):
            candidates.extend(
                [
                    artifacts.get("questions"),
                    artifacts.get("clarifying_questions"),
                ]
            )
        result = output.get("result")
        if isinstance(result, dict):
            candidates.extend(
                [
                    result.get("questions"),
                    result.get("clarifying_questions"),
                ]
            )
        return any(
            isinstance(candidate, list)
            and any(
                isinstance(item, str) and item.strip()
                for item in candidate
            )
            for candidate in candidates
        )

    def _extract_intake_questions(self, text: str) -> list[str]:
        questions: list[str] = []
        current: list[str] = []

        def flush() -> None:
            joined = " ".join(current).strip()
            if "?" in joined:
                questions.append(joined)
            current.clear()

        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            item = re.match(r"^(?:[-*]\s+|\d+[.)]\s+)", line)
            cleaned = line[item.end() :].strip() if item else line
            if not cleaned:
                continue
            if item:
                flush()
                current.append(cleaned)
            elif current and "?" not in " ".join(current):
                current.append(cleaned)
            elif "?" in cleaned:
                flush()
                current.append(cleaned[: cleaned.rindex("?") + 1])
        flush()

        return [question for question in questions if len(question) > 3]

    def _looks_like_prd_markdown(self, text: str) -> bool:
        if len(text.strip()) < 200:
            return False
        lowered = text.lower()
        if "prd" not in lowered and "product requirements" not in lowered:
            return False
        heading_count = sum(
            1
            for line in text.splitlines()
            if line.lstrip().startswith("#")
        )
        if heading_count < 3:
            return False
        section_markers = (
            "overview",
            "problem statement",
            "goals",
            "non-goals",
            "target audience",
            "core features",
            "user stories",
            "acceptance criteria",
            "success metrics",
            "open questions",
            "risks",
            "dependencies",
            "rollout plan",
            "timeline",
            "requirements",
        )
        return sum(1 for marker in section_markers if marker in lowered) >= 3

    def _prd_title_from_markdown(self, text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                if title:
                    return title
        return "PRD Draft"

    @staticmethod
    def _legacy_stage_validators_enabled() -> bool:
        """Legacy hard-coded stage validators stay on until backfill completes.

        Controlled by ``TS_HARNESS_LEGACY_STAGE_VALIDATORS`` (default ``1``).
        When a declarative contract is present it always takes precedence; this
        flag only governs stages that do not yet resolve a contract.
        """
        return os.environ.get("TS_HARNESS_LEGACY_STAGE_VALIDATORS", "1") != "0"

    @staticmethod
    def _enrich_coding_backend_args(
        args: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Fill qa_feedback/pr_url from task input when the model omits them."""
        task_input = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        enriched = dict(args)
        for key in ("qa_feedback", "pr_url", "program_id", "requested_by", "decision_id"):
            if str(enriched.get(key) or "").strip():
                continue
            value = task_input.get(key)
            if value:
                enriched[key] = value
        for key in ("workflow_id", "program_id"):
            if not str(enriched.get(key) or "").strip() and payload.get(key):
                enriched[key] = str(payload.get(key))
        return enriched

    @staticmethod
    def _contract_for_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return the resolved stage contract from the dispatch payload, if any."""
        contract = payload.get("contract")
        if isinstance(contract, dict) and contract:
            return contract
        return None

    def _output_repair_note(
        self,
        payload: dict[str, Any],
        validation_error: str,
        contract: dict[str, Any] | None,
    ) -> str:
        if contract is not None:
            return (
                "Your previous output failed the stage output contract and was "
                "rejected.\n"
                f"Error: {validation_error}\n"
                f"{stage_contracts.summarize_stage_contract(contract)}"
            )
        stage = self._work_orders_stage(payload)
        expected = sorted(self._work_orders_artifact_keys(stage)) if stage else []
        expected_note = (
            f" Required artifacts keys: {', '.join(expected)}." if expected else ""
        )
        return (
            "Your previous output failed workflow validation and was rejected.\n"
            f"Error: {validation_error}\n"
            "Return ONLY valid JSON for this workflow step."
            f"{expected_note} "
            "Do not return memory-recall summaries, action plans, "
            "or PRD recaps as artifacts. Produce distinct "
            "markdown bodies for each artifact key."
        )

    def _validate_workflow_output(
        self,
        output: dict[str, Any],
        payload: dict[str, Any],
        *,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if self._requires_prepare_handoff_output(payload):
            if self._has_prepare_handoff_output(output):
                return None
            return (
                "prepare_work_order_delivery output must include "
                "planner_complete, work_orders_handoff, work_order_ids, "
                "and next_work_order_id"
            )

        if self._requires_deliver_work_order_output(payload):
            return self._deliver_work_order_tool_failure_reason(output, tool_calls)

        if self._requires_work_orders_output(payload):
            if self._has_work_orders_artifacts(output, payload):
                return None
            stage = self._work_orders_stage(payload) or "stage"
            detail = work_orders_artifacts_failure_reason(
                output.get("artifacts") if isinstance(output.get("artifacts"), dict) else None,
                stage,
            )
            if detail:
                return (
                    "work_orders_from_prd output must include artifacts with "
                    f"completed {stage} content: {detail}"
                )
            return (
                "work_orders_from_prd output must include artifacts with "
                f"completed {stage} content"
            )

        if self._requires_intake_output(payload):
            if self._has_intake_questions(output):
                return None
            return (
                "intake output must include top-level questions "
                "with clarifying questions"
            )

        if not self._requires_prd_output(payload):
            return None

        markdown = output.get("prd_markdown")
        result = output.get("result")
        if not isinstance(markdown, str) and isinstance(result, dict):
            markdown = result.get("prd_markdown")
        if isinstance(markdown, str) and markdown.strip():
            return None
        return (
            "draft_prd output must include result.prd_markdown "
            "with the complete PRD markdown"
        )
