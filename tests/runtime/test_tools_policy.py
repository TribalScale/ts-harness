import stat
import sys
import tempfile
import unittest
from pathlib import Path

from ts_harness.config.schema import load_runtime_config
from ts_harness.tools.mcp_client import MCPClientManager
from ts_harness.config.schema import parse_runtime_config
from ts_harness.policy.engine import PolicyEngine
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy, redact_tool_error
from ts_harness.tools.registry import ToolRegistry


REPO_ROOT = Path(__file__).resolve().parents[2]


class ToolsPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")

    def test_subagent_cannot_delegate(self):
        agent = self.config.get_agent("dev-agent")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="dev-agent",
            source="subagent",
            is_subagent=True,
            parent_tools={"read_file", "exec"},
        )
        decision = evaluate_tool_policy("delegate_task", agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")

    def _stage_ctx(self, stage_tool_allow):
        return ToolPolicyContext(
            tenant_id="default",
            agent_id="pm-agent",
            source="workflow",
            stage_tool_allow=stage_tool_allow,
        )

    def test_stage_without_tool_allow_keeps_agent_grant(self):
        agent = self.config.get_agent("pm-agent")
        decision = evaluate_tool_policy("read_file", agent, self.config, self._stage_ctx(None))
        self.assertEqual(decision.decision, "allow")

    def test_stage_tool_allow_always_keeps_read_skill_reference(self):
        agent = self.config.get_agent("pm-agent")
        restricted = self._stage_ctx({"read_file"})
        self.assertEqual(
            evaluate_tool_policy("read_skill_reference", agent, self.config, restricted).decision, "allow"
        )

        from dataclasses import replace

        denied = replace(agent, tool_deny=[*agent.tool_deny, "read_skill_reference"])
        self.assertEqual(
            evaluate_tool_policy("read_skill_reference", denied, self.config, restricted).decision, "deny"
        )

    def test_stage_tool_allow_denies_unlisted_agent_tools(self):
        agent = self.config.get_agent("pm-agent")
        ctx = self._stage_ctx({"google_workspace_docs_append"})

        read = evaluate_tool_policy("read_file", agent, self.config, ctx)
        self.assertEqual((read.decision, read.policy_id), ("deny", "stage_tool_allow"))
        append = evaluate_tool_policy("google_workspace_docs_append", agent, self.config, ctx)
        self.assertEqual(append.decision, "allow")

    def test_stage_tool_allow_cannot_override_agent_deny(self):
        agent = self.config.get_agent("pm-agent")
        decision = evaluate_tool_policy("exec", agent, self.config, self._stage_ctx({"exec"}))
        self.assertEqual((decision.decision, decision.policy_id), ("deny", "agent_deny"))

    def test_tool_definitions_cache_is_per_stage_tool_allow(self):
        from ts_harness.tools.registry import build_default_registry

        registry = build_default_registry()
        agent = self.config.get_agent("pm-agent")

        def names(stage_tool_allow):
            defs = registry.definitions_for_agent(agent, self._stage_ctx(stage_tool_allow), self.config)
            return {item["function"]["name"] for item in defs}

        self.assertEqual(names({"read_file"}), {"read_file", "read_skill_reference"})
        self.assertEqual(names({"recall_memory"}), {"recall_memory", "read_skill_reference"})
        self.assertIn("workflow_update", names(None))

    def test_resumable_workflow_advertises_approval_required_tools(self):
        from ts_harness.tools.registry import build_default_registry

        registry = build_default_registry()
        agent = self.config.get_agent("dev-agent")

        def names(resumable):
            ctx = ToolPolicyContext(
                tenant_id="default",
                agent_id="dev-agent",
                source="workflow",
                stage_tool_allow={"read_file", "write_file", "delegate_task"},
                approval_resumable=resumable,
            )
            return {d["function"]["name"] for d in registry.definitions_for_agent(agent, ctx, self.config)}

        self.assertEqual(names(False), {"read_file", "read_skill_reference"})
        # delegate_task is on dev-agent's deny list, so it stays hidden.
        self.assertEqual(names(True), {"read_file", "write_file", "read_skill_reference"})

    def test_channel_ingress_blocks_exec(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
        )
        decision = evaluate_tool_policy("exec", agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")

    def test_channel_ingress_blocks_workflow_update(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
        )
        decision = evaluate_tool_policy("workflow_update", agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "channel_workflow_update")

    def test_channel_delegate_requires_confirmation(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            delegation_confirmed=False,
        )
        decision = evaluate_tool_policy("delegate_task", agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "channel_delegation")

    def test_channel_delegate_allowed_when_confirmed(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            delegation_confirmed=True,
        )
        decision = evaluate_tool_policy("delegate_task", agent, self.config, ctx)
        self.assertEqual(decision.decision, "allow")

    def test_propose_delegate_allowed_on_channel(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            session_id="sess-1",
        )
        decision = evaluate_tool_policy("propose_delegate_task", agent, self.config, ctx)
        self.assertEqual(decision.decision, "allow")

    def test_tool_allow_list_is_enforced(self):
        agent = self.config.get_agent("pm-agent")
        ctx = ToolPolicyContext(tenant_id="default", agent_id="pm-agent", source="workflow")
        decision = evaluate_tool_policy("exec", agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")

    def test_mcp_tools_are_allowed_when_agent_has_mcp_toolset(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "mcp_fixture.py"
            fixture.write_text(
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    req=json.loads(line)\n"
                "    result={'tools':[{'name':'search','description':'Search','inputSchema':{'type':'object'}}]}\n"
                "    print(json.dumps({'jsonrpc':'2.0','id':req.get('id'), 'result': result}), flush=True)\n"
            )
            fixture.chmod(fixture.stat().st_mode | stat.S_IXUSR)
            manager = MCPClientManager()
            registry = ToolRegistry()
            manager.register_server("atlassian", {"transport": "stdio", "command": sys.executable, "args": [str(fixture)]})
            manager.discover_tools("atlassian")
            manager.register_tools(registry)

            agent = self.config.get_agent("orchestrator")
            agent.toolsets = [*agent.toolsets, "mcp"]
            ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
            decision = evaluate_tool_policy("mcp-atlassian-search", agent, self.config, ctx)

        self.assertEqual(decision.decision, "allow")

    def test_policy_stages_run_in_deterministic_order(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(tenant_id="missing-tenant", agent_id="orchestrator", source="workflow")
        decision = evaluate_tool_policy("read_file", agent, self.config, ctx)
        self.assertEqual(decision.policy_id, "tenant_boundary")

    def test_redact_tool_error_never_leaks_secrets(self):
        message = "failed with super-secret-token Authorization: Bearer abc"
        redacted = redact_tool_error(message, level="high")
        self.assertNotIn("super-secret-token", redacted)
        self.assertNotIn("Bearer abc", redacted)

    def test_data_residency_denies_egress_tools(self):
        data = {
            "gateway": {"token": "x"},
            "tenants": [{"id": "eu-tenant", "display_name": "EU", "data_residency": "eu"}],
            "agents": {
                "list": [
                    {
                        "id": "orchestrator",
                        "default": True,
                        "workspace": "/w",
                        "model": "openai/gpt-4o-mini",
                        "tools": {"allow": ["mcp-atlassian-search"]},
                        "toolsets": ["mcp"],
                    }
                ]
            },
            "policy": {
                "data_residency_regions": {"eu-tenant": ["us"]},
                "egress_sensitive_tools": ["mcp"],
            },
        }
        config = parse_runtime_config(data)
        agent = config.get_agent("orchestrator")
        ctx = ToolPolicyContext(tenant_id="eu-tenant", agent_id="orchestrator", source="workflow")
        decision = evaluate_tool_policy("mcp-atlassian-search", agent, config, ctx)
        self.assertEqual(decision.policy_id, "data_residency")

    def test_rate_limit_denies_after_threshold(self):
        data = {
            "gateway": {"token": "x"},
            "agents": {
                "list": [
                    {
                        "id": "orchestrator",
                        "default": True,
                        "workspace": "/w",
                        "model": "openai/gpt-4o-mini",
                        "tools": {"allow": ["read_file"]},
                    }
                ]
            },
            "policy": {"rate_limit_per_minute": 2},
        }
        config = parse_runtime_config(data)
        agent = config.get_agent("orchestrator")
        engine = PolicyEngine(config)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
        self.assertEqual(evaluate_tool_policy("read_file", agent, config, ctx, policy_engine=engine).decision, "allow")
        self.assertEqual(evaluate_tool_policy("read_file", agent, config, ctx, policy_engine=engine).decision, "allow")
        decision = evaluate_tool_policy("read_file", agent, config, ctx, policy_engine=engine)
        self.assertEqual(decision.policy_id, "rate_limit")


    def test_start_workflow_requires_confirmation_on_channel(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            workflow_confirmed=False,
        )
        decision = evaluate_tool_policy("start_workflow", agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "channel_workflow_start")

    def test_start_workflow_denies_program_managed_types(self):
        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            workflow_confirmed=True,
            pending_workflow={
                "workflow_type": "jira_from_prd",
                "summary": "phase workflow",
            },
        )
        decision = evaluate_tool_policy(
            "start_workflow",
            agent,
            self.config,
            ctx,
            tool_args={"workflow_type": "jira_from_prd", "summary": "phase workflow"},
        )
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "channel_phase_workflow_start")

    def test_opa_can_deny_after_python_allows(self):
        from ts_harness.policy.opa import OpaClient

        agent = self.config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
        )
        opa = OpaClient(fail_closed=True)
        opa.evaluate_opa = lambda tool_name, policy_ctx: "deny" if tool_name == "read_file" else "allow"
        decision = evaluate_tool_policy(
            "read_file",
            agent,
            self.config,
            ctx,
            opa_client=opa,
        )
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "opa")


if __name__ == "__main__":
    unittest.main()
