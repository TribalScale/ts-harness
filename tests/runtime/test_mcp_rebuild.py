from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from ts_harness.config.mcp_settings import reload_mcp_settings_into_runtime
from ts_harness.gateway.server import RuntimeState
from ts_harness.tools.registry import ToolDefinition, ToolRegistry
from ts_harness.workflows.mcp_settings_store import McpSettingsStore
import ts_harness.workflows.mcp_settings_store as store_mod


class FakeMcpManager:
    def __init__(self):
        self.closed = False
        self.registered: list[str] = []
        self.discovered: list[str] = []

    def close_all(self) -> None:
        self.closed = True
        self.registered = []
        self.discovered = []

    def register_server(self, name: str, config: dict) -> None:
        self.registered.append(name)

    def discover_tools(self, name: str) -> list[dict]:
        if name == "bad":
            raise RuntimeError("discover failed")
        self.discovered.append(name)
        return [{"name": "ping"}]

    def register_tools(self, registry: ToolRegistry) -> None:
        for name in self.discovered:
            registry.register(
                ToolDefinition(
                    name=f"mcp-{name}-ping",
                    description="ping",
                    parameters={"type": "object"},
                    handler=lambda args, ctx: {},
                    toolsets=["mcp"],
                ),
                override=True,
            )


class McpRebuildTests(unittest.TestCase):
    def tearDown(self):
        store_mod._STORE = None

    def test_rebuild_lists_degraded_and_keeps_both_servers(self):
        runtime = SimpleNamespace(
            config=SimpleNamespace(
                mcp_servers={
                    "good": {"transport": "sse", "url": "https://good.example/mcp", "enabled": True},
                    "bad": {"transport": "sse", "url": "https://bad.example/mcp", "enabled": True},
                }
            ),
            mcp=FakeMcpManager(),
            registry=ToolRegistry(),
        )
        result = RuntimeState.rebuild_mcp_runtime(runtime)
        self.assertEqual(result["degraded"], ["bad"])
        self.assertIn("good", runtime.config.mcp_servers)
        self.assertIn("bad", runtime.config.mcp_servers)
        self.assertIn("mcp-good-ping", runtime.registry.snapshot())
        self.assertNotIn("mcp-bad-ping", runtime.registry.snapshot())

    def test_rebuild_lists_unhealthy_discover_without_raise(self):
        class QuietFail:
            def __init__(self):
                self._servers = {}

            def close_all(self):
                self._servers.clear()

            def register_server(self, name, config):
                self._servers[name] = SimpleNamespace(healthy=True, config=config, tools=[])

            def discover_tools(self, name):
                self._servers[name].healthy = False
                return []

            def server_state(self, name):
                return self._servers[name]

            def register_tools(self, registry):
                return None

        runtime = SimpleNamespace(
            config=SimpleNamespace(
                mcp_servers={"quiet": {"transport": "sse", "url": "https://quiet.example/mcp", "enabled": True}}
            ),
            mcp=QuietFail(),
            registry=ToolRegistry(),
            secret_resolver=None,
        )
        result = RuntimeState.rebuild_mcp_runtime(runtime)
        self.assertEqual(result["degraded"], ["quiet"])

    def test_rebuild_resolves_secret_refs_before_register(self):
        class Capturing:
            def __init__(self):
                self.registered = []
                self._servers = {}

            def close_all(self):
                self._servers.clear()

            def register_server(self, name, config):
                self.registered.append((name, dict(config)))
                self._servers[name] = SimpleNamespace(healthy=True, config=config, tools=[])

            def discover_tools(self, name):
                return []

            def register_tools(self, registry):
                return None

        runtime = SimpleNamespace(
            config=SimpleNamespace(
                mcp_servers={
                    "figma": {
                        "transport": "streamable_http",
                        "url": "https://mcp.example/mcp",
                        "enabled": True,
                        "headers": {"Authorization": "secret:FIGMA"},
                    }
                }
            ),
            mcp=Capturing(),
            registry=ToolRegistry(),
            secret_resolver=SimpleNamespace(resolve=lambda name: f"resolved-{name}"),
        )
        RuntimeState.rebuild_mcp_runtime(runtime)
        name, config = runtime.mcp.registered[0]
        self.assertEqual(name, "figma")
        self.assertEqual(config["headers"]["Authorization"], "resolved-FIGMA")

    def test_boot_applies_active_overlay_onto_yaml_baseline(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            store = McpSettingsStore(Path(tmp) / "mcp-settings")
            store_mod._STORE = store
            draft = store.create_draft(
                {
                    "definition_type": "mcp_settings",
                    "definition_version": "1.0.0",
                    "servers": {
                        "figma": {
                            "transport": "streamable_http",
                            "url": "https://mcp.figma.com/mcp",
                            "enabled": True,
                            "headers": {"Authorization": "secret:FIGMA_MCP_TOKEN"},
                        }
                    },
                },
                actor_id="admin",
            )
            store.activate(draft["id"], expected_revision=draft["revision"], actor_id="admin")
            os.environ["TS_HARNESS_MCP_SETTINGS_STORE"] = str(Path(tmp) / "mcp-settings")
            os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
            os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
            os.environ["SKILLS_DIR"] = str(repo_root / "skills")
            runtime = None
            try:
                runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
                self.assertIn("atlassian", runtime._yaml_mcp_baseline)
                self.assertNotIn("figma", runtime._yaml_mcp_baseline)
                self.assertIn("atlassian", runtime.config.mcp_servers)
                self.assertEqual(
                    runtime.config.mcp_servers["figma"]["url"],
                    "https://mcp.figma.com/mcp",
                )
            finally:
                if runtime is not None:
                    runtime.shutdown()
                os.environ.pop("TS_HARNESS_MCP_SETTINGS_STORE", None)
                os.environ.pop("TS_HARNESS_SKIP_MCP_DISCOVERY", None)
                os.environ.pop("RUNTIME_FAKE_PROVIDER", None)

    def test_boot_resolves_secret_refs_before_register(self):
        repo_root = Path(__file__).resolve().parents[2]
        env = {
            "TS_HARNESS_SKIP_MCP_DISCOVERY": "true",
            "RUNTIME_FAKE_PROVIDER": "true",
            "SKILLS_DIR": str(repo_root / "skills"),
            "ATLASSIAN_SITE_URL": "https://example.atlassian.net",
        }
        saved = {key: os.environ.get(key) for key in (*env, "ATLASSIAN_CLIENT_ID")}
        os.environ.update(env)
        os.environ.pop("ATLASSIAN_CLIENT_ID", None)
        runtime = None
        try:
            runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
            server_env = runtime.mcp.server_state("atlassian").config["env"]
            self.assertEqual(server_env["ATLASSIAN_SITE_URL"], "https://example.atlassian.net")
            self.assertEqual(server_env["ATLASSIAN_CLIENT_ID"], "")
            self.assertFalse([key for key, value in server_env.items() if value.startswith("secret:")])
        finally:
            if runtime is not None:
                runtime.shutdown()
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ReloadMcpSettingsTests(unittest.TestCase):
    def tearDown(self):
        store_mod._STORE = None

    def test_reload_merges_active_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = McpSettingsStore(Path(tmp))
            store_mod._STORE = store
            draft = store.create_draft(
                {
                    "definition_type": "mcp_settings",
                    "definition_version": "1.0.0",
                    "servers": {
                        "atlassian": {"enabled": False},
                    },
                },
                actor_id="admin",
            )
            store.activate(draft["id"], expected_revision=draft["revision"], actor_id="admin")
            runtime = MagicMock()
            runtime.config.mcp_servers = {
                "atlassian": {"transport": "stdio", "command": "mcporter", "enabled": True}
            }
            runtime._yaml_mcp_baseline = {
                "atlassian": {"transport": "stdio", "command": "mcporter", "enabled": True}
            }
            self.assertTrue(reload_mcp_settings_into_runtime(runtime))
            self.assertFalse(runtime.config.mcp_servers["atlassian"]["enabled"])
            self.assertEqual(runtime.config.mcp_servers["atlassian"]["command"], "mcporter")


if __name__ == "__main__":
    unittest.main()
