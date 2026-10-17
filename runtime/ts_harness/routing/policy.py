"""Config- and intent-driven tool policy for channel ingress routing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ts_harness.routing.matcher import find_routing_rule

if TYPE_CHECKING:
    from ts_harness.config.schema import RoutingIntentPolicy, RoutingIntentRule, RuntimeConfig
    from ts_harness.tools.policy import PolicyDecision, ToolPolicyContext


def evaluate_routing_intent_policy(
    tool_name: str,
    ctx: ToolPolicyContext,
    config: RuntimeConfig,
    tool_args: dict[str, Any] | None = None,
) -> PolicyDecision | None:
    from ts_harness.tools.policy import PolicyDecision

    intent = str(ctx.channel_intent or "")
    skip_intents = {"workflow_gate", "continue_delivery", "continue_bridge", "direct_respond"}
    if not intent or intent in skip_intents:
        if intent == "direct_respond" and tool_name in {
            "propose_delegate_task",
            "delegate_task",
            "propose_start_workflow",
            "start_workflow",
        }:
            return PolicyDecision(
                "deny",
                "direct-respond intent should not delegate or start workflows",
                "intent_direct_respond",
                "medium",
            )
        return None

    rule = find_routing_rule(config, intent)
    if rule is None:
        return None

    args = tool_args or {}
    if ctx.workflow_linked or ctx.informal_prd_opt_out:
        return None

    policy = effective_rule_policy(rule)
    return evaluate_rule_policy(tool_name, ctx, args, rule=rule, policy=policy)


def effective_rule_policy(rule: RoutingIntentRule) -> RoutingIntentPolicy:
    from ts_harness.config.schema import RoutingIntentPolicy

    if rule.policy is not None:
        return rule.policy
    if rule.program_type or rule.workflow_type:
        return RoutingIntentPolicy(
            mode="require_workflow",
            deny_tools=["propose_delegate_task", "delegate_task"],
        )
    if rule.delegate_agent:
        deny_types = [rule.workflow_type] if rule.workflow_type else []
        return RoutingIntentPolicy(mode="require_delegate", deny_start_workflow_types=deny_types)
    return RoutingIntentPolicy(mode="require_workflow")


def evaluate_rule_policy(
    tool_name: str,
    ctx: ToolPolicyContext,
    args: dict[str, Any],
    *,
    rule: RoutingIntentRule,
    policy: RoutingIntentPolicy,
) -> PolicyDecision | None:
    from ts_harness.tools.policy import PolicyDecision

    policy_id = f"intent_{rule.id}"
    workflow_type = _workflow_type_for_tool(ctx, args)
    program_type = _program_type_for_tool(ctx, args)

    if policy.mode == "require_workflow":
        allowed_programs = [str(item) for item in policy.allow_program_types or []]
        if tool_name in set(policy.deny_tools):
            if allowed_programs:
                required = "propose_start_program for " + " or ".join(allowed_programs)
            elif rule.program_type:
                required = f"propose_start_program(program_type={rule.program_type})"
            else:
                required = f"propose_start_workflow({rule.workflow_type or 'configured workflow'})"
            return PolicyDecision(
                "deny",
                f"{rule.id} requests must use {required}",
                policy_id,
                "medium",
            )
        if allowed_programs and tool_name in {"propose_start_program", "start_program"}:
            if program_type not in set(allowed_programs):
                return PolicyDecision(
                    "deny",
                    f"{rule.id} intent requires program_type one of {', '.join(allowed_programs)}",
                    policy_id,
                    "medium",
                )
            return None
        if rule.program_type and not rule.workflow_type:
            if tool_name in {"propose_start_workflow", "start_workflow"}:
                return PolicyDecision(
                    "deny",
                    f"{rule.id} intent requires propose_start_program(program_type={rule.program_type})",
                    policy_id,
                    "medium",
                )
            if tool_name == "propose_start_program":
                if not program_type or program_type != rule.program_type:
                    return PolicyDecision(
                        "deny",
                        f"{rule.id} intent requires program_type={rule.program_type}",
                        policy_id,
                        "medium",
                    )
            return None
        if tool_name == "propose_start_workflow":
            if not workflow_type or workflow_type != rule.workflow_type:
                return PolicyDecision(
                    "deny",
                    f"{rule.id} intent requires workflow_type={rule.workflow_type}",
                    policy_id,
                    "medium",
                )
        if tool_name == "start_workflow" and workflow_type in set(policy.deny_start_workflow_types):
            return PolicyDecision(
                "deny",
                f"{workflow_type} cannot be started directly for intent {rule.id}",
                policy_id,
                "medium",
            )
        return None

    if policy.mode == "require_delegate":
        delegate_agent = str(rule.delegate_agent or "")
        deny_types = set(policy.deny_start_workflow_types)
        if tool_name in {"propose_start_workflow", "start_workflow"} and workflow_type in deny_types:
            return PolicyDecision(
                "deny",
                f"{policy_id} should delegate to {delegate_agent}, not start workflow",
                policy_id,
                "medium",
            )
        if tool_name == "propose_start_workflow" and deny_types and not workflow_type:
            return PolicyDecision(
                "deny",
                f"{policy_id} should use propose_delegate_task({delegate_agent})",
                policy_id,
                "medium",
            )
        if tool_name in {"propose_delegate_task", "delegate_task"}:
            agent_id = _delegate_agent_for_tool(ctx, args)
            if agent_id and delegate_agent and agent_id != delegate_agent:
                return PolicyDecision(
                    "deny",
                    f"{policy_id} requires delegate_agent={delegate_agent}",
                    policy_id,
                    "medium",
                )
        return None

    return None


def _workflow_type_for_tool(ctx: ToolPolicyContext, args: dict[str, Any]) -> str:
    pending = ctx.pending_workflow or {}
    return str(args.get("workflow_type") or pending.get("workflow_type") or "")


def _program_type_for_tool(ctx: ToolPolicyContext, args: dict[str, Any]) -> str:
    pending = getattr(ctx, "pending_program", None) or {}
    return str(args.get("program_type") or pending.get("program_type") or "")


def _delegate_agent_for_tool(ctx: ToolPolicyContext, args: dict[str, Any]) -> str:
    pending = ctx.pending_delegation or {}
    return str(args.get("agent_id") or pending.get("agent_id") or "")
