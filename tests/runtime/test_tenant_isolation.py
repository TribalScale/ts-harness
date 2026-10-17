"""Cross-tenant isolation integration tests."""

from __future__ import annotations

import unittest
import unittest.mock

from ts_harness.agent.sessions import SessionStore
from ts_harness.audit.writer import AuditWriter
from ts_harness.config.schema import load_runtime_config
from ts_harness.memory.client import MemoryClient
from ts_harness.sandbox.docker_env import DockerExecutionEnvironment
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


class TenantIsolationTests(unittest.TestCase):
    def test_sessions_isolated_by_tenant(self):
        store = SessionStore()
        store.get_or_create(tenant_id="tenant-a", source="workflow", agent_id="orchestrator")
        store.get_or_create(tenant_id="tenant-b", source="workflow", agent_id="orchestrator")
        self.assertEqual(len(store.list_for_tenant("tenant-a")), 1)
        self.assertEqual(len(store.list_for_tenant("tenant-b")), 1)
        self.assertNotEqual(
            store.list_for_tenant("tenant-a")[0].session_id,
            store.list_for_tenant("tenant-b")[0].session_id,
        )

    def test_audit_export_scoped_per_tenant(self):
        audit = AuditWriter()
        audit.append(tenant_id="tenant-a", action_type="a", actor_id="u1")
        audit.append(tenant_id="tenant-b", action_type="b", actor_id="u2")
        self.assertEqual(len(audit.export(tenant_id="tenant-a")), 1)
        self.assertEqual(audit.export(tenant_id="tenant-a")[0]["tenant_id"], "tenant-a")

    def test_memory_client_scopes_by_tenant(self):
        client = MemoryClient("http://memory-api")
        calls = []

        def fake_request(method, path, body=None):
            calls.append(body)
            return {"status": "ok"}

        with unittest.mock.patch.object(client, "_request", side_effect=fake_request):
            client.remember(tenant_id="tenant-a", agent_id="orchestrator", content="note-a")
            client.remember(tenant_id="tenant-b", agent_id="orchestrator", content="note-b")

        self.assertEqual(calls[0]["scope"], "tenant:tenant-a:agent:orchestrator")
        self.assertEqual(calls[1]["scope"], "tenant:tenant-b:agent:orchestrator")

    def test_memory_client_project_scope_is_opt_in(self):
        client = MemoryClient("http://memory-api")
        calls = []

        def fake_request(method, path, body=None):
            calls.append(body)
            return {"status": "ok"}

        with unittest.mock.patch.object(client, "_request", side_effect=fake_request):
            client.remember(
                tenant_id="tenant-a",
                agent_id="orchestrator",
                content="shared fact",
                scope="project:TFS",
            )
            client.recall(
                tenant_id="tenant-a",
                agent_id="orchestrator",
                query="fact",
                scope="project:TFS",
            )

        self.assertEqual(calls[0]["scope"], "project:TFS")
        self.assertEqual(calls[0]["tenant_id"], "tenant-a")
        self.assertEqual(calls[1]["scope"], "project:TFS")
        self.assertEqual(calls[1]["tenant_id"], "tenant-a")

    def test_unknown_tenant_denied_by_policy(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = config.get_agent("orchestrator")
        ctx = ToolPolicyContext(tenant_id="unknown-tenant", agent_id="orchestrator", source="workflow")
        decision = evaluate_tool_policy("read_file", agent, config, ctx)
        self.assertEqual(decision.decision, "deny")

    def test_mcp_denied_when_connector_disabled(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        config.tenants[0].enabled_connectors = []
        agent = config.get_agent("orchestrator")
        agent.tool_allow = ["mcp-atlassian-search"]
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="workflow")
        decision = evaluate_tool_policy("mcp-atlassian-search", agent, config, ctx)
        self.assertEqual(decision.policy_id, "mcp_connector")

    def test_sandbox_labels_tenant_scope(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        env = DockerExecutionEnvironment(enabled=True, runner=runner)
        env.run("echo ok", workspace="/workspace", tenant_id="tenant-z")
        self.assertIn("ts-harness.tenant=tenant-z", captured["cmd"])


if __name__ == "__main__":
    unittest.main()
