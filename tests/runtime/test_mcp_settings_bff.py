from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.mcp_settings_bff import (
    activate_mcp_settings_admin,
    create_mcp_settings_draft_admin,
    get_mcp_settings_admin,
    probe_mcp_server_admin,
    validate_mcp_settings_draft_admin,
)
from ts_harness.workflows.mcp_settings_store import McpSettingsStore
import ts_harness.workflows.mcp_settings_store as store_mod


def _auth() -> AuthContext:
    return AuthContext(actor_id="admin", tenant_id="default", roles=["admin"])


def _runtime() -> mock.MagicMock:
    runtime = mock.MagicMock()
    runtime.config.mcp_servers = {
        "atlassian": {
            "transport": "stdio",
            "command": "mcporter",
            "enabled": True,
            "env": {"ATLASSIAN_TOKEN": "secret:ATLASSIAN_TOKEN"},
        }
    }
    runtime._yaml_mcp_baseline = {
        "atlassian": {
            "transport": "stdio",
            "command": "mcporter",
            "enabled": True,
            "env": {"ATLASSIAN_TOKEN": "secret:ATLASSIAN_TOKEN"},
        }
    }
    runtime.secret_resolver = mock.MagicMock()
    runtime.secret_resolver.resolve.return_value = "resolved-secret"
    runtime.rebuild_mcp_runtime.return_value = {"degraded": []}
    runtime.mcp = object()
    return runtime


class McpSettingsBffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_mod._STORE = McpSettingsStore(Path(self.tmp.name))

    def tearDown(self):
        store_mod._STORE = None
        self.tmp.cleanup()

    def test_get_redacts_env(self):
        payload = get_mcp_settings_admin(_runtime())
        self.assertEqual(payload["merged"]["atlassian"]["env"]["ATLASSIAN_TOKEN"], "***")
        self.assertEqual(payload["yaml"]["atlassian"]["env"]["ATLASSIAN_TOKEN"], "***")
        self.assertNotIn("resolved-secret", str(payload))

    def test_validate_blocks_freeform_command(self):
        with self.assertRaises(ValueError) as raised:
            create_mcp_settings_draft_admin(
                _runtime(),
                _auth(),
                {
                    "definition": {
                        "definition_type": "mcp_settings",
                        "definition_version": "1.0.0",
                        "servers": {
                            "evil": {
                                "transport": "stdio",
                                "command": "bash",
                                "args": ["-c", "whoami"],
                            }
                        },
                    }
                },
            )
        self.assertIn("catalog", str(raised.exception).lower())
        created = create_mcp_settings_draft_admin(
            _runtime(),
            _auth(),
            {
                "definition": {
                    "definition_type": "mcp_settings",
                    "definition_version": "1.0.0",
                    "servers": {},
                }
            },
        )
        result = validate_mcp_settings_draft_admin(created["draft"]["id"])
        self.assertTrue(result["valid"])

    def test_activate_rebuilds(self):
        runtime = _runtime()
        created = create_mcp_settings_draft_admin(
            runtime,
            _auth(),
            {
                "definition": {
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
                }
            },
        )
        draft = created["draft"]
        result = activate_mcp_settings_admin(
            runtime,
            _auth(),
            {"draft_id": draft["id"], "expected_revision": draft["revision"]},
        )
        self.assertTrue(result["reload_ok"])
        runtime.rebuild_mcp_runtime.assert_called_once()
        self.assertEqual(
            runtime.config.mcp_servers["figma"]["url"],
            "https://mcp.figma.com/mcp",
        )
        self.assertEqual(runtime.config.mcp_servers["atlassian"]["command"], "mcporter")

    def test_test_uses_draft_merge_and_leaves_manager(self):
        runtime = _runtime()
        live = runtime.mcp
        created = create_mcp_settings_draft_admin(
            runtime,
            _auth(),
            {
                "definition": {
                    "definition_type": "mcp_settings",
                    "definition_version": "1.0.0",
                    "servers": {
                        "figma": {
                            "transport": "sse",
                            "url": "https://draft.example/mcp",
                            "enabled": True,
                            "headers": {"Authorization": "secret:FIGMA_MCP_TOKEN"},
                        }
                    },
                }
            },
        )
        self.assertTrue(created["draft"]["id"])
        fake = mock.MagicMock()
        fake.discover_tools.return_value = [{"name": "mcp-figma-ping"}]
        fake.server_state.return_value = SimpleNamespace(healthy=True, config={})
        with mock.patch(
            "ts_harness.gateway.mcp_settings_bff.MCPClientManager",
            return_value=fake,
        ):
            result = probe_mcp_server_admin(runtime, "figma")
        self.assertTrue(result["ok"])
        fake.register_server.assert_called_once()
        registered = fake.register_server.call_args[0]
        self.assertEqual(registered[0], "figma")
        self.assertEqual(registered[1]["url"], "https://draft.example/mcp")
        self.assertEqual(registered[1]["headers"]["Authorization"], "resolved-secret")
        self.assertIs(runtime.mcp, live)
        fake.close_all.assert_called_once()


class McpSettingsAsgiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_mod._STORE = McpSettingsStore(Path(self.tmp.name))
        self.runtime = _runtime()
        self.auth = mock.Mock(actor_id="admin", roles=["admin"], tenant_id="default")
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(decision="allow")
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        store_mod._STORE = None
        self.tmp.cleanup()

    def test_non_admin_forbidden(self):
        self.auth.roles = ["member"]
        self.runtime.policy.evaluate_identity.return_value = mock.Mock(
            decision="deny",
            reason="role denied",
        )
        response = self.client.post("/admin/mcp-settings/drafts", json={})
        self.assertEqual(response.status_code, 403)

    def test_get_and_update_draft_round_trip(self):
        created = self.client.post(
            "/admin/mcp-settings/drafts",
            json={
                "definition": {
                    "definition_type": "mcp_settings",
                    "definition_version": "1.0.0",
                    "servers": {},
                }
            },
        )
        self.assertEqual(created.status_code, 200)
        draft = created.json()["draft"]
        updated = self.client.put(
            f"/admin/mcp-settings/drafts/{draft['id']}",
            json={
                "expected_revision": draft["revision"],
                "definition": {
                    "definition_type": "mcp_settings",
                    "definition_version": "1.0.0",
                    "servers": {
                        "figma": {
                            "transport": "streamable_http",
                            "url": "https://mcp.figma.com/mcp",
                            "enabled": True,
                        }
                    },
                },
            },
        )
        self.assertEqual(updated.status_code, 200)
        payload = self.client.get("/admin/mcp-settings")
        self.assertEqual(payload.status_code, 200)
        self.assertEqual(payload.json()["sources"]["figma"], "overlay")
        catalog = self.client.get("/admin/mcp/catalog")
        self.assertEqual(catalog.status_code, 200)
        self.assertTrue(any(entry["id"] == "atlassian" for entry in catalog.json()["entries"]))


if __name__ == "__main__":
    unittest.main()
