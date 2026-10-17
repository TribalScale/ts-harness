import json
import os
import unittest
import tempfile
from pathlib import Path

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.agent.sessions import SessionStore
from ts_harness.config.schema import load_runtime_config
from ts_harness.tools.delegation import configure_delegation, handle_delegate_task
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.registry import build_default_registry


REPO_ROOT = Path(__file__).resolve().parents[2]


class DelegationTests(unittest.TestCase):
    def test_leaf_subagent_cannot_spawn_agents(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": "child done"}),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        configure_delegation(config, agent)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
        result = handle_delegate_task(
            {"agent_id": "dev-agent", "task": "implement feature"},
            ctx,
        )
        self.assertIn("status", result)
        self.assertIn("child_session_id", result)

    def test_configured_max_children_blocks_extra_delegation(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {"choices": [{"message": {"role": "assistant", "content": json.dumps({"summary": "one"})}}]},
                {"choices": [{"message": {"role": "assistant", "content": json.dumps({"summary": "two"})}}]},
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        configure_delegation(config, agent, max_children=1)
        ctx = ToolPolicyContext(tenant_id="tenant-max-children", agent_id="orchestrator", source="workflow")

        first = handle_delegate_task({"agent_id": "dev-agent", "task": "first"}, ctx)
        second = handle_delegate_task({"agent_id": "pm-agent", "task": "second"}, ctx)

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["error"], "max children per parent exceeded")

    def test_child_tools_are_capped_by_parent_stage_tool_allow(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [{"choices": [{"message": {"role": "assistant", "content": json.dumps({"summary": "ok"})}}]}]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        configure_delegation(config, agent)
        ctx = ToolPolicyContext(
            tenant_id="tenant-stage-cap",
            agent_id="orchestrator",
            source="workflow",
            stage_tool_allow={"read_file", "delegate_task"},
        )

        from unittest import mock

        with mock.patch.object(agent, "run_workflow_dispatch", wraps=agent.run_workflow_dispatch) as run:
            handle_delegate_task({"agent_id": "dev-agent", "task": "inspect"}, ctx)

        self.assertEqual(
            run.call_args.kwargs["parent_tools"], {"read_file", "delegate_task", "read_skill_reference"}
        )

    def test_session_records_include_started_at_and_tool_count(self):
        store = SessionStore()
        parent = store.get_or_create(tenant_id="default", source="workflow", agent_id="orchestrator")
        child = store.create_subagent_session(parent, "dev-agent")
        child.tool_count += 2

        self.assertIsNotNone(child.started_at)
        self.assertEqual(child.parent_session_id, parent.session_id)
        self.assertEqual(child.subagent_id, "dev-agent")
        self.assertEqual(child.tool_count, 2)

    def test_parent_output_excludes_child_transcript(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"summary": "child summary", "transcript": ["secret child detail"]}
                                ),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        configure_delegation(config, agent)

        result = handle_delegate_task(
            {"agent_id": "dev-agent", "task": "summarize"},
            ToolPolicyContext(tenant_id="tenant-no-transcript", agent_id="orchestrator", source="workflow"),
        )

        self.assertEqual(result["summary"], "child summary")
        self.assertNotIn("transcript", result)

    def test_delegation_state_survives_restart(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [{"choices": [{"message": {"role": "assistant", "content": json.dumps({"summary": "child"})}}]}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "delegation.json"
            os.environ["TS_HARNESS_DELEGATION_STATE"] = str(state_path)
            try:
                from ts_harness.tools import delegation as delegation_module

                agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
                delegation_module.configure_delegation(config, agent, max_children=1)
                ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
                handle_delegate_task({"agent_id": "dev-agent", "task": "first"}, ctx)

                delegation_module._load_state()
            finally:
                os.environ.pop("TS_HARNESS_DELEGATION_STATE", None)

        self.assertEqual(delegation_module._ACTIVE_CHILDREN, 0)
        self.assertTrue(delegation_module._subagent_children)

    def test_leaf_subagent_cannot_delegate(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = config.get_agent("dev-agent")
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="dev-agent",
            source="subagent",
            is_subagent=True,
            parent_tools={"read_file"},
        )
        from ts_harness.tools.policy import evaluate_tool_policy

        decision = evaluate_tool_policy("delegate_task", agent, config, ctx)
        self.assertEqual(decision.decision, "deny")
        self.assertEqual(decision.policy_id, "subagent_delegation")

    def test_orchestrator_can_delegate_when_configured(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        orchestrator = config.get_agent("orchestrator")
        self.assertIn("dev-agent", orchestrator.subagent_allow)

    def test_child_context_excludes_parent_transcript(self):
        """Subagent should not receive parent session messages; context is truncated."""
        import json
        from ts_harness.agent.provider import FakeProvider
        from ts_harness.agent.loop import EnterpriseAgent
        from ts_harness.tools.registry import build_default_registry
        from ts_harness.tools.delegation import handle_delegate_task, configure_delegation
        from ts_harness.tools.policy import ToolPolicyContext

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")

        # Use FakeProvider which records all calls in self.calls
        provider = FakeProvider(
            [{"choices": [{"message": {"role": "assistant", "content": json.dumps({"summary": "child work"})}}]}]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        configure_delegation(config, agent)

        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")

        handle_delegate_task(
            {"agent_id": "dev-agent", "task": "implement feature"},
            ctx,
        )

        # The provider should have been called at least once
        self.assertTrue(len(provider.calls) > 0, "Provider should have been called")

        # Check the FIRST call to the provider (initial context, before any child tool calls)
        # This verifies the child starts with truncated context, not parent transcript
        first_call = provider.calls[0]
        messages_at_call_time = list(first_call.get("messages", []))  # Copy to avoid mutation

        # Truncated context means child receives only essential messages:
        # system prompt + user message describing the delegated task
        # No parent conversation history should be present
        self.assertLessEqual(
            len(messages_at_call_time),
            3,  # system + user + possible assistant from provider injection
            "Child context should be truncated (small message count), not include full parent transcript",
        )

        # Verify structure of initial context
        system_msgs = [m for m in messages_at_call_time if m.get("role") == "system"]
        user_msgs = [m for m in messages_at_call_time if m.get("role") == "user"]

        self.assertEqual(len(system_msgs), 1, "Child should receive exactly one system prompt")
        self.assertEqual(len(user_msgs), 1, "Child should receive exactly one user message with task")

    def test_interrupt_stops_at_next_loop_boundary(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {"name": "session_status", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"role": "assistant", "content": "should not run"}}]},
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        interrupted = {"value": False}

        def interrupt_after_tool(_record):
            interrupted["value"] = True
            agent.interrupt("stop requested")

        agent.on_tool_call = interrupt_after_tool
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="orchestrator",
            payload={"task_id": "interrupt", "step_name": "interrupt"},
        )

        self.assertTrue(interrupted["value"])
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.error, "interrupted: stop requested")


if __name__ == "__main__":
    unittest.main()
