"""Policy tests for google-workspace connector tools."""

from __future__ import annotations



from ts_harness.config.schema import AgentConfig, PolicyConfig, RuntimeConfig, TenantConfig, GatewayConfig
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy


def _config(*, enabled: bool) -> RuntimeConfig:
    connectors = ["google-workspace"] if enabled else []
    return RuntimeConfig(
        tenants=[TenantConfig(id="default", display_name="Default", enabled_connectors=connectors)],
        agents=[
            AgentConfig(
                id="comms-agent",
                workspace="/w",
                model="openai/gpt-4o-mini",
                default=True,
                tool_allow=["google_workspace_gmail_search"],
                toolsets=["google-workspace"],
            )
        ],
        gateway=GatewayConfig(token="t"),
        policy=PolicyConfig(approval_required_tools=["google_workspace_gmail_send"]),
    )


def test_google_workspace_denied_when_connector_disabled():
    agent = _config(enabled=False).agents[0]
    config = _config(enabled=False)
    ctx = ToolPolicyContext(tenant_id="default", agent_id="comms-agent", source="workflow")
    decision = evaluate_tool_policy("google_workspace_gmail_search", agent, config, ctx)
    assert decision.decision == "deny"
    assert decision.policy_id == "google_workspace_connector"


def test_google_workspace_allowed_when_enabled():
    agent = _config(enabled=True).agents[0]
    config = _config(enabled=True)
    ctx = ToolPolicyContext(tenant_id="default", agent_id="comms-agent", source="workflow")
    decision = evaluate_tool_policy("google_workspace_gmail_search", agent, config, ctx)
    assert decision.decision == "allow"


def test_gmail_send_requires_approval():
    agent = AgentConfig(
        id="comms-agent",
        workspace="/w",
        model="m",
        default=True,
        tool_allow=["google_workspace_gmail_send"],
        toolsets=["google-workspace"],
    )
    config = RuntimeConfig(
        tenants=[TenantConfig(id="default", display_name="Default", enabled_connectors=["google-workspace"])],
        agents=[agent],
        gateway=GatewayConfig(token="t"),
        policy=PolicyConfig(approval_required_tools=["google_workspace_gmail_send"]),
    )
    ctx = ToolPolicyContext(tenant_id="default", agent_id="comms-agent", source="workflow")
    decision = evaluate_tool_policy("google_workspace_gmail_send", agent, config, ctx)
    assert decision.decision == "approval_required"


def test_gmail_send_allowed_with_approval_id():
    from ts_harness.tools.approvals import ApprovalQueue

    agent = AgentConfig(
        id="comms-agent",
        workspace="/w",
        model="m",
        default=True,
        tool_allow=["google_workspace_gmail_send"],
        toolsets=["google-workspace"],
    )
    config = RuntimeConfig(
        tenants=[TenantConfig(id="default", display_name="Default", enabled_connectors=["google-workspace"])],
        agents=[agent],
        gateway=GatewayConfig(token="t"),
        policy=PolicyConfig(approval_required_tools=["google_workspace_gmail_send"]),
    )
    args = {"to": "a@example.com", "subject": "Hi", "body": "Hello"}
    queue = ApprovalQueue()
    req = queue.request(
        tenant_id="default",
        agent_id="comms-agent",
        tool_name="google_workspace_gmail_send",
        args=args,
        reason="send requires approval",
    )
    queue.decide(req.approval_id, approved=True, actor_id="admin")
    from ts_harness.tools import approvals as approvals_mod

    original = approvals_mod._approval_queue
    try:
        approvals_mod._approval_queue = queue
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="comms-agent",
            source="workflow",
            approval_id=req.approval_id,
        )
        decision = evaluate_tool_policy(
            "google_workspace_gmail_send",
            agent,
            config,
            ctx,
            tool_args=args,
        )
        assert decision.decision == "allow"
        assert decision.policy_id == "approval_consumed"
    finally:
        approvals_mod._approval_queue = original
