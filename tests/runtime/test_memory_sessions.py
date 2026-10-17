import json
import os
import unittest
import tempfile
from pathlib import Path

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.agent.sessions import SessionStore
from ts_harness.config.schema import load_runtime_config
from ts_harness.tools.registry import build_default_registry
from ts_harness.memory.client import MemoryClient
from ts_harness.tools.approvals import ApprovalQueue


class SessionTests(unittest.TestCase):
    def test_routing_keys_are_deterministic(self):
        store = SessionStore()
        a = store.routing_key(tenant_id="default", source="slack", channel="slack", thread_id="123")
        b = store.routing_key(tenant_id="default", source="slack", channel="slack", thread_id="123")
        self.assertEqual(a, b)

    def test_routing_keys_differ_by_tenant(self):
        store = SessionStore()
        a = store.routing_key(tenant_id="tenant-a", source="slack", channel="slack", thread_id="123")
        b = store.routing_key(tenant_id="tenant-b", source="slack", channel="slack", thread_id="123")
        self.assertNotEqual(a, b)

    def test_discord_telegram_and_cron_routing_keys_are_stable(self):
        store = SessionStore()
        for source in ("discord", "telegram", "cron"):
            a = store.routing_key(tenant_id="default", source=source, channel=source, thread_id="t-1")
            b = store.routing_key(tenant_id="default", source=source, channel=source, thread_id="t-1")
            self.assertEqual(a, b, source)

    def test_subagent_parent_child_link(self):
        store = SessionStore()
        parent = store.get_or_create(tenant_id="default", source="workflow", agent_id="orchestrator", workflow_id="wf-1")
        child = store.create_subagent_session(parent, "dev-agent")
        self.assertEqual(child.parent_session_id, parent.session_id)
        self.assertEqual(child.depth, 1)

    def test_session_store_recovers_records_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.jsonl"
            first = SessionStore(path=path)
            session = first.get_or_create(
                tenant_id="default", source="workflow", agent_id="orchestrator", workflow_id="wf-1"
            )
            session.tool_count = 3
            first.save(session)

            second = SessionStore(path=path)
            recovered = second.get(session.session_id)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.tool_count, 3)
        self.assertEqual(recovered.routing_key, session.routing_key)

    def test_approval_queue_recovers_pending_requests_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.jsonl"
            first = ApprovalQueue(path=path)
            approval = first.request(
                tenant_id="default",
                agent_id="dev-agent",
                tool_name="exec",
                args={"command": "deploy"},
                reason="approval required",
            )

            second = ApprovalQueue(path=path)
            recovered = second.get(approval.approval_id)

        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.tool_name, "exec")

    def test_agent_run_persists_tool_count_and_status(self):
        repo_root = Path(__file__).resolve().parents[2]
        config = load_runtime_config(repo_root / "config" / "ts-harness-runtime.test.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.jsonl"
            os.environ["TS_HARNESS_SESSION_STORE"] = str(path)
            try:
                from ts_harness.agent import sessions as sessions_module

                sessions_module._default_store = SessionStore(path=path)
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
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": json.dumps({"summary": "done"}),
                                    }
                                }
                            ]
                        },
                    ]
                )
                agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
                agent.run_workflow_dispatch(
                    tenant_id="default",
                    agent_id="orchestrator",
                    payload={"task_id": "persist", "step_name": "persist"},
                )
                reloaded = SessionStore(path=path)
                session = next(iter(reloaded._by_id.values()))
            finally:
                os.environ.pop("TS_HARNESS_SESSION_STORE", None)
                sessions_module._default_store = SessionStore(None)

        self.assertEqual(session.tool_count, 1)
        self.assertEqual(session.status, "completed")

    def test_memory_client_uses_memory_api_prefixed_routes(self):
        calls = []

        class Client(MemoryClient):
            def _request(self, method, path, body=None):
                calls.append((method, path, body))
                return {"results": []}

        client = Client("http://memory")
        client.remember(tenant_id="t", agent_id="a", content="note")
        client.recall(tenant_id="t", agent_id="a", query="note")
        client.forget(tenant_id="t", memory_id="m")

        self.assertEqual([call[1] for call in calls], ["/memory/remember", "/memory/recall", "/memory/forget"])

    def test_recall_handler_uses_the_program_department_only(self):
        from types import SimpleNamespace
        from unittest import mock

        from ts_harness.memory.client import recall_memory_handler
        from ts_harness.tools.policy import ToolPolicyContext

        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
            session_id="s1",
        )
        session = SimpleNamespace(tenant_id="default", metadata={"program_id": "p1"})
        client = mock.Mock()
        client.get_program.return_value = {
            "program": {"tenant_id": "default", "department": "engineering"}
        }
        client.recall.return_value = []
        with mock.patch("ts_harness.memory.client.get_memory_client", return_value=client), mock.patch(
            "ts_harness.agent.sessions.get_session_store"
        ) as store:
            store.return_value.get.return_value = session
            recall_memory_handler({"query": "stack", "department_scope": "department:sales"}, ctx)
        self.assertEqual(
            client.recall.call_args.kwargs["department_scope"],
            "department:engineering",
        )


if __name__ == "__main__":
    unittest.main()
