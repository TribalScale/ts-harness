from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ts_harness.workflows.mcp_settings_store import McpSettingsStore


def _definition(**overrides):
    payload = {
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
    payload.update(overrides)
    return payload


class McpSettingsStoreTests(unittest.TestCase):
    def test_create_activate_and_get_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = McpSettingsStore(Path(tmp))
            draft = store.create_draft(_definition(), actor_id="admin")
            self.assertEqual(len(store.list_drafts()), 1)
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["definition_type"], "mcp_settings")
            active = store.get_active("mcp_settings")
            self.assertEqual(active["servers"]["figma"]["url"], "https://mcp.figma.com/mcp")
            self.assertEqual(store.list_drafts(), [])

    def test_invalid_payload_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = McpSettingsStore(Path(tmp))
            with self.assertRaises(ValueError):
                store.create_draft(
                    {
                        "definition_type": "runtime_settings",
                        "definition_version": "1.0.0",
                        "servers": {},
                    },
                    actor_id="admin",
                )


if __name__ == "__main__":
    unittest.main()
