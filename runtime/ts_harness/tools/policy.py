"""TS Harness-inspired tool policy pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ts_harness.config.schema import GLOBAL_DENY_TOOLS, AgentConfig, RuntimeConfig
from ts_harness.policy.engine import PolicyEngine
from ts_harness.tools.toolsets import agent_effective_tools

if TYPE_CHECKING:
    from ts_harness.policy.opa import OpaClient


DecisionType = Literal["allow", "deny", "approval_required"]


def _intent_routing_enabled() -> bool:
    import os

    return os.environ.get("TS_HARNESS_INTENT_ROUTING", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# Read-only access to the stage's own assigned skills; a stage tool restriction never removes it.
STAGE_ALWAYS_ALLOWED_TOOLS = frozenset({"read_skill_reference"})

# Tools whose effect reaches other people (send, publish, change shared records). Every
# ``mcp-`` tool counts too; see ``publish_tool_names``. Docs create/append are drafting
# tools and stay out: stages draft into Docs before their approval gate.
PUBLISH_TOOLS = frozenset(
    {
        "channel_send",
        "google_workspace_gmail_send",
        "google_workspace_calendar_create",
        "google_workspace_calendar_update",
        "google_workspace_calendar_cancel",
    }
)


JIRA_WRITE_EFFECT = "jira_write"
# MCP tools that only read; every other ``mcp-`` tool counts as a publish tool.
READ_ONLY_MCP_TOOLS = frozenset(
    {
        "mcp-atlassian-atlassian_search",
        "mcp-atlassian-jira_search",
        "mcp-atlassian-jira_get_issue",
        "mcp-atlassian-jira_get_all_projects",
    }
)


def is_mcp_tool(tool_name: str) -> bool:
    return str(tool_name).startswith("mcp-")


def publish_tool_names(candidates: set[str], config: RuntimeConfig) -> set[str]:
    """Publish tools among ``candidates`` that do not already pause for Inbox approval."""
    approval_gated = set(config.policy.approval_required_tools)
    return {
        name
        for name in candidates
        if (name in PUBLISH_TOOLS or (is_mcp_tool(name) and name not in READ_ONLY_MCP_TOOLS))
        and name not in approval_gated
    }


@dataclass
class PolicyDecision:
    decision: DecisionType
    reason: str
    policy_id: str
    redaction_level: str = "none"


@dataclass
class ToolPolicyContext:
    tenant_id: str
    agent_id: str
    source: str
    is_subagent: bool = False
    parent_tools: set[str] | None = None
    channel_ingress: bool = False
    sandbox_restricted: bool = False
    model_id: str | None = None
    requires_egress: bool = False
    session_id: str | None = None
    delegation_confirmed: bool = False
    pending_delegation: dict | None = None
    workflow_confirmed: bool = False
    pending_workflow: dict | None = None
    workflow_linked: bool = False
    linked_workflow_id: str | None = None
    continue_delivery_confirmed: bool = False
    bridge_confirmed: bool = False
    pending_continue_delivery: dict | None = None
    pending_bridge: dict | None = None
    prd_phase: str | None = None
    workflow_phase: str | None = None
    channel_intent: str | None = None
    informal_prd_opt_out: bool = False
    session_mode: str | None = None
    bridge_available: bool = False
    actor_email: str | None = None
    approval_id: str | None = None
    program_confirmed: bool = False
    pending_program: dict | None = None
    program_advance_confirmed: bool = False
    pending_program_advance: dict | None = None
    # Set only for workflow stage dispatch; None means the agent grant applies unchanged.
    stage_tool_allow: set[str] | None = None
    # The stage's allowed_external_effects; None outside a workflow stage.
    stage_external_effects: set[str] | None = None
    # True only when an approval_required call can pause and later resume this run.
    approval_resumable: bool = False


def _auto_delegate_allowed(config: RuntimeConfig, ctx: ToolPolicyContext) -> bool:
    auto_agents = set(config.policy.delegation_auto_allow_agents or [])
    pending = ctx.pending_delegation or {}
    agent_id = str(pending.get("agent_id") or "")
    task_text = str(pending.get("task") or "").lower()
    mutating = any(
        keyword in task_text
        for keyword in ("deploy", "exec", "merge", "jira", "push", "delete", "notify", "post")
    )
    return bool(pending) and agent_id in auto_agents and not mutating


def evaluate_tool_policy(
    tool_name: str,
    agent: AgentConfig,
    config: RuntimeConfig,
    ctx: ToolPolicyContext,
    *,
    policy_engine: PolicyEngine | None = None,
    opa_client: "OpaClient | None" = None,
    tool_args: dict | None = None,
) -> PolicyDecision:
    if not ctx.tenant_id:
        return PolicyDecision("deny", "tenant required", "tenant_boundary", "high")

    tenant_ids = {tenant.id for tenant in config.tenants}
    if ctx.tenant_id not in tenant_ids:
        return PolicyDecision("deny", "unknown tenant", "tenant_boundary", "high")

    if tool_name in GLOBAL_DENY_TOOLS:
        return PolicyDecision("deny", "globally denied tool", "global_deny", "high")

    from ts_harness.config.bindings import (
        check_jira_projects_allowed,
        is_jira_write_tool,
        jira_project_keys_from_args,
        tenant_bindings,
    )

    if is_jira_write_tool(tool_name) and ctx.stage_external_effects is not None:
        if JIRA_WRITE_EFFECT not in ctx.stage_external_effects:
            return PolicyDecision(
                "deny",
                "workflow stage does not allow Jira writes",
                "stage_external_effects",
                "medium",
            )

    # Without args the call is only being advertised; the binding is checked at dispatch.
    if is_jira_write_tool(tool_name) and tool_args is not None:
        bindings = tenant_bindings(getattr(config, "program_defaults", None))
        projects = jira_project_keys_from_args(tool_args)
        if bindings.jira_allowed_projects and not projects:
            return PolicyDecision(
                "deny",
                "Jira write must name a project or issue key in the tenant binding",
                "tenant_jira_binding",
                "medium",
            )
        binding_error = check_jira_projects_allowed(projects, bindings)
        if binding_error:
            return PolicyDecision("deny", binding_error, "tenant_jira_binding", "medium")

    tenant = next((t for t in config.tenants if t.id == ctx.tenant_id), None)
    if tenant and config.policy.data_residency_regions:
        allowed_regions = config.policy.data_residency_regions.get(ctx.tenant_id)
        if allowed_regions and tenant.data_residency not in allowed_regions:
            egress_tools = set(config.policy.egress_sensitive_tools)
            if tool_name in egress_tools or tool_name.startswith("mcp-") or tool_name.startswith(
                "google_workspace_"
            ):
                return PolicyDecision(
                    "deny",
                    "data residency restriction",
                    "data_residency",
                    "high",
                )

    if ctx.source != "workflow" and tool_name in set(config.policy.network_egress_deny_tools):
        return PolicyDecision("deny", "network egress blocked for source", "network_egress", "high")

    if ctx.model_id and config.policy.model_allowlist:
        allowed_models = config.policy.model_allowlist.get(ctx.agent_id)
        if allowed_models and ctx.model_id not in allowed_models:
            return PolicyDecision("deny", "model not allowlisted for agent", "model_restrictions", "medium")

    if policy_engine is not None and config.policy.rate_limit_per_minute > 0:
        rate = policy_engine.evaluate_rate_limit(
            tenant_id=ctx.tenant_id,
            tool_name=tool_name,
            limit=config.policy.rate_limit_per_minute,
        )
        if rate.decision == "deny":
            return PolicyDecision("deny", rate.reason, rate.policy_id, rate.redaction_level)

    if ctx.channel_ingress and tool_name == "workflow_update":
        return PolicyDecision(
            "deny",
            "workflow_update requires workflow dispatch",
            "channel_workflow_update",
            "medium",
        )

    if ctx.channel_ingress and tool_name == "delegate_task":
        if not ctx.delegation_confirmed and not _auto_delegate_allowed(config, ctx):
            return PolicyDecision(
                "deny",
                "delegate_task requires user confirmation",
                "channel_delegation",
                "medium",
            )

    if ctx.channel_ingress and tool_name == "start_workflow":
        if not ctx.workflow_confirmed:
            return PolicyDecision(
                "deny",
                "start_workflow requires user confirmation",
                "channel_workflow_start",
                "medium",
            )
        if ctx.linked_workflow_id:
            return PolicyDecision(
                "deny",
                "session already linked to a workflow",
                "channel_workflow_duplicate",
                "medium",
            )
        pending_type = str((ctx.pending_workflow or {}).get("workflow_type") or "")
        from ts_harness.tools.workflow_tools import is_program_managed_workflow

        workflow_type = str((tool_args or {}).get("workflow_type") or pending_type)
        if is_program_managed_workflow(workflow_type):
            return PolicyDecision(
                "deny",
                f"{workflow_type} must start via start_program or bridge_workflow",
                "channel_phase_workflow_start",
                "medium",
            )

    if ctx.channel_ingress and tool_name == "start_program":
        if not ctx.program_confirmed:
            return PolicyDecision(
                "deny",
                "start_program requires user confirmation",
                "channel_program_start",
                "medium",
            )

    bridge_confirmed = ctx.bridge_confirmed or ctx.continue_delivery_confirmed or ctx.program_advance_confirmed
    workflow_phase = ctx.workflow_phase or ctx.prd_phase
    session_mode = ctx.session_mode or ""

    if ctx.channel_ingress and tool_name == "advance_program":
        if not ctx.program_advance_confirmed and not bridge_confirmed:
            return PolicyDecision(
                "deny",
                "advance_program requires user confirmation",
                "channel_program_advance",
                "medium",
            )

    bridge_tools = {"bridge_workflow", "continue_delivery", "advance_program"}
    propose_bridge_tools = {"propose_bridge_workflow", "propose_continue_delivery", "propose_advance_program"}
    if ctx.channel_ingress and tool_name in bridge_tools:
        if tool_name == "advance_program":
            if not (ctx.program_advance_confirmed or bridge_confirmed):
                return PolicyDecision(
                    "deny",
                    "advance_program requires user confirmation",
                    "channel_program_advance",
                    "medium",
                )
        elif not bridge_confirmed:
            return PolicyDecision(
                "deny",
                "bridge_workflow requires user confirmation",
                "channel_bridge",
                "medium",
            )
        if session_mode in {"bridge_linked", "delivery_linked"}:
            return PolicyDecision(
                "deny",
                "session already linked to bridge target workflow",
                "channel_bridge_duplicate",
                "medium",
            )

    if ctx.channel_ingress and tool_name in propose_bridge_tools:
        if workflow_phase != "approved" or session_mode not in {"workflow_linked", "prd_linked"}:
            return PolicyDecision(
                "deny",
                "propose_bridge_workflow requires approved linked workflow",
                "channel_bridge_propose",
                "medium",
            )
        if not ctx.bridge_available:
            return PolicyDecision(
                "deny",
                "no bridge configured for linked workflow",
                "channel_bridge_propose",
                "medium",
            )
        if session_mode in {"bridge_linked", "delivery_linked"}:
            return PolicyDecision(
                "deny",
                "session already linked to bridge target workflow",
                "channel_bridge_duplicate",
                "medium",
            )

    if ctx.channel_ingress and tool_name == "continue_delivery":
        if not bridge_confirmed:
            return PolicyDecision(
                "deny",
                "continue_delivery requires user confirmation",
                "channel_continue_delivery",
                "medium",
            )
        if session_mode in {"bridge_linked", "delivery_linked"}:
            return PolicyDecision(
                "deny",
                "session already linked to delivery workflow",
                "channel_delivery_duplicate",
                "medium",
            )

    if ctx.channel_ingress and tool_name == "propose_continue_delivery":
        if workflow_phase != "approved" or session_mode not in {"workflow_linked", "prd_linked"}:
            return PolicyDecision(
                "deny",
                "propose_continue_delivery requires approved PRD workflow",
                "channel_continue_delivery_propose",
                "medium",
            )
        if session_mode in {"bridge_linked", "delivery_linked"}:
            return PolicyDecision(
                "deny",
                "session already linked to delivery workflow",
                "channel_delivery_duplicate",
                "medium",
            )

    if ctx.channel_ingress and _intent_routing_enabled():
        from ts_harness.routing.policy import evaluate_routing_intent_policy

        routing_decision = evaluate_routing_intent_policy(
            tool_name,
            ctx,
            config,
            tool_args=tool_args,
        )
        if routing_decision is not None:
            return routing_decision

    if ctx.channel_ingress and tool_name in {
        "workflow_approve",
        "workflow_reject",
        "workflow_reject_with_feedback",
        "cancel_workflow",
        "resume_external_event",
    }:
        if not ctx.workflow_linked or not ctx.linked_workflow_id:
            return PolicyDecision(
                "deny",
                "workflow gate tools require a linked workflow session",
                "channel_workflow_gate",
                "medium",
            )

    if ctx.channel_ingress and tool_name in set(config.policy.channel_ingress_deny):
        return PolicyDecision("deny", "blocked for channel ingress", "channel_ingress", "medium")

    if ctx.is_subagent and tool_name in {"delegate_task", "delegation"}:
        return PolicyDecision("deny", "recursive delegation blocked", "subagent_delegation", "high")

    if agent.tool_deny and tool_name in set(agent.tool_deny):
        return PolicyDecision("deny", "denied by agent policy", "agent_deny", "medium")

    effective_tools = agent_effective_tools(agent, config)
    if agent.tool_allow and tool_name not in effective_tools:
        return PolicyDecision("deny", "not in agent allow list", "agent_allow", "medium")

    if (
        ctx.stage_tool_allow is not None
        and tool_name not in ctx.stage_tool_allow
        and tool_name not in STAGE_ALWAYS_ALLOWED_TOOLS
    ):
        return PolicyDecision("deny", "not in workflow stage tool allow", "stage_tool_allow", "medium")

    if ctx.is_subagent and ctx.parent_tools is not None and tool_name not in ctx.parent_tools:
        return PolicyDecision("deny", "subagent cannot exceed parent tools", "subagent_intersection", "high")

    if ctx.sandbox_restricted and tool_name in {
        "exec",
        "write_file",
        "patch_file",
        "propose_skill",
    }:
        return PolicyDecision("deny", "sandbox restrictions apply", "sandbox", "medium")

    if tool_name.startswith("mcp-"):
        tenant = next((t for t in config.tenants if t.id == ctx.tenant_id), None)
        if tenant and "mcp" not in tenant.enabled_connectors and not any(
            c.startswith("mcp") for c in tenant.enabled_connectors
        ):
            return PolicyDecision("deny", "mcp connector disabled for tenant", "mcp_connector", "high")

    if tool_name.startswith("google_workspace_"):
        tenant = next((t for t in config.tenants if t.id == ctx.tenant_id), None)
        if tenant and "google-workspace" not in tenant.enabled_connectors:
            return PolicyDecision(
                "deny",
                "google-workspace connector disabled for tenant",
                "google_workspace_connector",
                "high",
            )

    if policy_engine is not None:
        budget = policy_engine.evaluate_budget(tenant_id=ctx.tenant_id, cost_usd=0.0)
        if budget.decision == "deny":
            return PolicyDecision("deny", budget.reason, budget.policy_id, budget.redaction_level)
        memory = policy_engine.evaluate_memory_access(tenant_id=ctx.tenant_id, action="tool")
        if memory.decision == "deny":
            return PolicyDecision("deny", memory.reason, memory.policy_id, memory.redaction_level)
        tool = policy_engine.evaluate_tool(tool_name, tenant_id=ctx.tenant_id, agent_id=ctx.agent_id, source=ctx.source)
        if tool.decision == "deny":
            return PolicyDecision("deny", tool.reason, tool.policy_id, tool.redaction_level)
        if tool.decision == "approval_required":
            if not (
                (
                    ctx.channel_ingress
                    and ctx.delegation_confirmed
                    and tool_name == "delegate_task"
                )
                or (
                    ctx.channel_ingress
                    and ctx.workflow_confirmed
                    and tool_name == "start_workflow"
                )
                or (
                    ctx.channel_ingress
                    and bridge_confirmed
                    and tool_name in {"continue_delivery", "bridge_workflow"}
                )
                or (
                    ctx.channel_ingress
                    and tool_name == "delegate_task"
                    and _auto_delegate_allowed(config, ctx)
                )
            ):
                return PolicyDecision("approval_required", tool.reason, tool.policy_id, tool.redaction_level)

    approval_id = (ctx.approval_id or "").strip() or None
    if not approval_id and tool_args:
        raw_id = tool_args.get("approval_id")
        if raw_id:
            approval_id = str(raw_id).strip() or None
    if approval_id:
        from ts_harness.tools.approvals import get_approval_queue

        clean_args = {k: v for k, v in (tool_args or {}).items() if k != "approval_id"}
        if get_approval_queue().is_approved_and_matches(
            approval_id,
            tool_name=tool_name,
            args=clean_args,
            tenant_id=ctx.tenant_id,
            agent_id=ctx.agent_id,
        ):
            return PolicyDecision("allow", "prior approval matched", "approval_consumed", "low")

    if tool_name in set(config.policy.approval_required_tools):
        if not (
            (
                ctx.channel_ingress
                and ctx.delegation_confirmed
                and tool_name == "delegate_task"
            )
            or (
                ctx.channel_ingress
                and ctx.workflow_confirmed
                and tool_name == "start_workflow"
            )
            or (
                ctx.channel_ingress
                and bridge_confirmed
                and tool_name in {"continue_delivery", "bridge_workflow"}
            )
            or (
                ctx.channel_ingress
                and tool_name == "delegate_task"
                and _auto_delegate_allowed(config, ctx)
            )
        ):
            return PolicyDecision("approval_required", "human approval required", "approval_required", "low")

    # OPA is the final gate before the default allow.  It is only consulted when
    # an OpaClient is passed; all earlier Python rules take precedence.
    if opa_client is not None:
        opa_decision = opa_client.evaluate_opa(tool_name, ctx)
        if opa_decision == "deny":
            return PolicyDecision("deny", "denied by OPA policy", "opa", "medium")

    return PolicyDecision("allow", "allowed", "default_allow", "none")


def redact_tool_error(message: str, *, level: str = "medium") -> str:
    if level == "high":
        return "tool execution blocked by policy"
    if level == "medium":
        return message[:120]
    return message[:200]


_BEARER_RE = re.compile(r"Authorization:\s*Bearer\s+\S+", re.IGNORECASE)
_SECRET_ASSIGN_RE = re.compile(r"(?i)(token|password|secret|api[_-]?key)=\S+")


def redact_snippet(text: str) -> str:
    redacted = str(text or "")
    redacted = _BEARER_RE.sub("Authorization: Bearer [REDACTED]", redacted)
    redacted = _SECRET_ASSIGN_RE.sub(r"\1=[REDACTED]", redacted)
    return redacted[:240]
