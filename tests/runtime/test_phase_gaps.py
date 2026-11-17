"""Regression tests for phase 1-12 gap closure."""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ts_harness.agent.sessions import SessionStore
from ts_harness.config.reload import default_lkg_path, reload_runtime_config, save_last_known_good
from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.server import RuntimeState
from ts_harness.gateway.auth import AuthError, check_allowed_origin
from ts_harness.gateway.channels.slack import SlackAdapter
from ts_harness.gateway import server as gateway_server
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.channels.base import MessageEvent
from ts_harness.gateway.channels.router import ChannelRouter
from ts_harness.sandbox.docker_env import DockerExecutionEnvironment
from ts_harness.tools.audit import configure_tool_audit, get_tool_audit_log
from ts_harness.tools.core_tools import register_core_tools
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy
from ts_harness.tools.registry import ToolRegistry


REPO_ROOT = Path(__file__).resolve().parents[2]


class PhaseGapTests(unittest.TestCase):
    def test_allowed_origin_enforced(self):
        with self.assertRaises(AuthError):
            check_allowed_origin({"Origin": "https://evil.example"}, ["http://localhost"])

    def test_runtime_state_boots_from_lkg_when_config_invalid(self):
        good = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "runtime.yaml"
            lkg = default_lkg_path(bad)
            bad.write_text("gateway:\n  token: ''\nagents:\n  list: []\n")
            save_last_known_good(good, lkg)
            os.environ["TS_HARNESS_CONFIG"] = str(bad)
            os.environ["TS_HARNESS_CONFIG_LKG"] = str(lkg)
            os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
            os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
            os.environ["SKILLS_DIR"] = str(REPO_ROOT / "skills")
            gateway_server._RUNTIME = None
            try:
                runtime = RuntimeState(str(bad))
                self.assertEqual(runtime.config.default_agent().id, "orchestrator")
            finally:
                gateway_server._RUNTIME = None
                os.environ.pop("TS_HARNESS_CONFIG", None)
                os.environ.pop("TS_HARNESS_CONFIG_LKG", None)
                os.environ.pop("TS_HARNESS_SKIP_MCP_DISCOVERY", None)
                os.environ.pop("RUNTIME_FAKE_PROVIDER", None)

    def test_config_reload_falls_back_to_last_known_good(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "runtime.yaml"
            lkg = Path(tmp) / "runtime.yaml.lkg"
            bad.write_text("gateway:\n  token: ''\nagents:\n  list: []\n")
            save_last_known_good(config, lkg)
            recovered = reload_runtime_config(bad, lkg_path=lkg)
        self.assertEqual(recovered.default_agent().id, "orchestrator")

    def test_slack_channel_allowlist_blocks_unauthorized_channel(self):
        os.environ["SLACK_ALLOWED_CHANNELS"] = "C_ALLOWED"
        try:
            adapter = SlackAdapter(token="x")
            with self.assertRaises(ValueError):
                adapter.normalize_inbound(
                    {"event_id": "e1", "channel": "C_BLOCKED", "user": "U1", "text": "hi"},
                    tenant_id="t",
                )
        finally:
            os.environ.pop("SLACK_ALLOWED_CHANNELS", None)

    def test_sandbox_strict_mode_denies_local_fallback(self):
        os.environ["SANDBOX_STRICT"] = "true"
        try:
            env = DockerExecutionEnvironment(enabled=False)
            result = env.run("echo ok", workspace="/tmp", tenant_id="default")
        finally:
            os.environ.pop("SANDBOX_STRICT", None)
        self.assertEqual(result.get("backend"), "denied")

    def test_tool_audit_sink_records_events(self):
        events = []
        configure_tool_audit(lambda event: events.append(event))
        get_tool_audit_log().record(
            tenant_id="default",
            agent_id="orchestrator",
            tool_name="exec",
            decision="deny",
            policy_id="sandbox",
        )
        self.assertEqual(len(events), 1)

    def test_read_skill_reference_tool_registered(self):
        registry = ToolRegistry()
        register_core_tools(registry)
        names = set(registry.snapshot())
        self.assertIn("read_skill_reference", names)

    def test_session_store_scopes_by_tenant(self):
        store = SessionStore()
        store.get_or_create(tenant_id="tenant-a", source="workflow", agent_id="orchestrator")
        store.get_or_create(tenant_id="tenant-b", source="workflow", agent_id="orchestrator")
        self.assertEqual(len(store.list_for_tenant("tenant-a")), 1)
        self.assertEqual(len(store.list_for_tenant("tenant-b")), 1)

    def test_channel_webhook_sends_reply(self):
        os.environ["TS_HARNESS_CONFIG"] = str(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        gateway_server._RUNTIME = None
        try:
            runtime = gateway_server.get_runtime()
            sent = []

            class FakeSlackAdapter:
                def normalize_inbound(self, payload, *, tenant_id: str) -> MessageEvent:
                    return MessageEvent(
                        channel="slack",
                        tenant_id=tenant_id,
                        text=str(payload.get("text", "")),
                        sender_id=str(payload.get("user", "U1")),
                        metadata={"channel_id": payload.get("channel", "C123")},
                    )

                def send(self, args, *, tenant_id: str):
                    sent.append((args, tenant_id))
                    return {"status": "sent", "channel": "slack"}

            with unittest.mock.patch.object(
                gateway_server,
                "get_channel_router",
                lambda: ChannelRouter(adapters={"slack": FakeSlackAdapter()}),
            ):
                result = runtime.handle_channel_webhook(
                    "slack",
                    {"event_id": "reply-event", "channel": "C123", "user": "U1", "text": "hello"},
                    AuthContext(actor_id="U1", tenant_id="default"),
                )

            self.assertEqual(result["reply"]["status"], "sent")
            self.assertEqual(sent[0][0]["channel"], "C123")
        finally:
            if gateway_server._RUNTIME is not None:
                gateway_server._RUNTIME.shutdown()
            gateway_server._RUNTIME = None
            os.environ.pop("RUNTIME_FAKE_PROVIDER", None)
            os.environ.pop("TS_HARNESS_SKIP_MCP_DISCOVERY", None)
            os.environ.pop("TS_HARNESS_CONFIG", None)

    def test_unknown_tenant_denied_by_policy(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = config.get_agent("orchestrator")
        ctx = ToolPolicyContext(tenant_id="missing-tenant", agent_id="orchestrator", source="workflow")
        decision = evaluate_tool_policy("read_file", agent, config, ctx)
        self.assertEqual(decision.decision, "deny")


if __name__ == "__main__":
    unittest.main()
