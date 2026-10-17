from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ts_harness.workflows.agents_overlay_store import AgentsOverlayStore


def _definition(**overrides):
    payload = {
        "definition_type": "agents",
        "definition_version": "1.0.0",
        "agents": {
            "legal-agent": {
                "workspace": "/workspace/legal-agent",
                "model": "openai/gpt-4o",
                "display_name": "Legal",
                "soul": "Cautious counsel.",
                "skills": ["ops"],
                "toolsets": ["core-read"],
                "tools": {"allow": ["recall_memory"], "deny": ["exec"]},
                "default": False,
                "delegatable": True,
            }
        },
    }
    payload.update(overrides)
    return payload


class AgentsOverlayStoreTests(unittest.TestCase):
    def test_create_activate_and_get_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentsOverlayStore(Path(tmp))
            draft = store.create_draft(_definition(), actor_id="admin")
            self.assertEqual(len(store.list_drafts()), 1)
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["definition_type"], "agents")
            active = store.get_active("agents")
            self.assertEqual(active["agents"]["legal-agent"]["model"], "openai/gpt-4o")
            self.assertEqual(store.list_drafts(), [])

    def test_create_draft_is_structural_and_does_not_need_runtime_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentsOverlayStore(Path(tmp))
            draft = store.create_draft(
                _definition(
                    agents={
                        "legal-agent": {
                            "workspace": "/workspace/legal-agent",
                            "model": "openai/gpt-4o",
                            "skills": ["ops"],
                            "toolsets": ["only-on-live-runtime"],
                            "default": False,
                        }
                    }
                ),
                actor_id="admin",
            )
            self.assertEqual(
                draft["definition"]["agents"]["legal-agent"]["toolsets"],
                ["only-on-live-runtime"],
            )

    def test_invalid_payload_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentsOverlayStore(Path(tmp))
            with self.assertRaises(ValueError):
                store.create_draft(
                    {
                        "definition_type": "mcp_settings",
                        "definition_version": "1.0.0",
                        "agents": {},
                    },
                    actor_id="admin",
                )


if __name__ == "__main__":
    unittest.main()
