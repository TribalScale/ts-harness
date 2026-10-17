"""Policy enforcement for governed PRD intent routing."""

from __future__ import annotations

import os
import unittest

from ts_harness.config.schema import load_runtime_config
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy


class IntentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = load_runtime_config("config/ts-harness-runtime.test.yaml")
        self.agent = self.config.get_agent("orchestrator")
        self._prev = os.environ.get("TS_HARNESS_INTENT_ROUTING")
        os.environ["TS_HARNESS_INTENT_ROUTING"] = "true"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("TS_HARNESS_INTENT_ROUTING", None)
        else:
            os.environ["TS_HARNESS_INTENT_ROUTING"] = self._prev

    def test_delegate_denied_on_governed_prd_intent(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            channel_intent="governed_prd",
            workflow_linked=False,
        )
        decision = evaluate_tool_policy("propose_delegate_task", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "intent_governed_prd")

    def test_informal_prd_allows_delegate(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            channel_intent="informal_prd",
            informal_prd_opt_out=True,
            workflow_linked=False,
        )
        decision = evaluate_tool_policy("propose_delegate_task", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "allow")

    def test_start_governed_delivery_denied(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            workflow_confirmed=True,
            pending_workflow={"workflow_type": "governed_delivery", "summary": "full flow"},
        )
        decision = evaluate_tool_policy("start_workflow", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")

    def test_propose_wrong_workflow_type_denied_on_first_call(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            channel_intent="governed_prd",
            workflow_linked=False,
        )
        decision = evaluate_tool_policy(
            "propose_start_workflow",
            self.agent,
            self.config,
            ctx,
            tool_args={"workflow_type": "governed_delivery", "summary": "full flow"},
        )
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "intent_governed_prd")

    def test_propose_missing_workflow_type_denied_on_governed_prd(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            channel_intent="governed_prd",
            workflow_linked=False,
        )
        decision = evaluate_tool_policy(
            "propose_start_workflow",
            self.agent,
            self.config,
            ctx,
            tool_args={"summary": "Create PRD from requirements"},
        )
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "intent_governed_prd")

    def test_direct_respond_denies_delegation(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            channel_intent="direct_respond",
        )
        decision = evaluate_tool_policy("propose_delegate_task", self.agent, self.config, ctx)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "intent_direct_respond")

    def test_code_intake_denies_delegate_and_limits_programs(self):
        config = load_runtime_config("config/ts-harness-runtime.test.yaml")
        agent = config.get_agent("orchestrator")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            channel_intent="code_intake",
            workflow_linked=False,
        )
        denied = evaluate_tool_policy(
            "propose_delegate_task",
            agent,
            config,
            ctx,
            tool_args={"agent_id": "dev-agent", "task": "change the checkout"},
        )
        self.assertEqual(denied.decision, "deny")
        self.assertEqual(denied.policy_id, "intent_code_intake")
        wrong_program = evaluate_tool_policy(
            "propose_start_program",
            agent,
            config,
            ctx,
            tool_args={"program_type": "software_delivery"},
        )
        self.assertEqual(wrong_program.decision, "deny")
        for program_type in ("bug_to_jira_work_orders", "content_to_jira_work_orders"):
            allowed = evaluate_tool_policy(
                "propose_start_program",
                agent,
                config,
                ctx,
                tool_args={"program_type": program_type},
            )
            self.assertNotEqual(allowed.decision, "deny", msg=program_type)


if __name__ == "__main__":
    unittest.main()
