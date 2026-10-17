"""Parity and restart tests for frozen legacy program compatibility."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.repo_paths import ensure_scripts_importable, programs_dir
from ts_harness.workflows.program_registry import ProgramDefinitionRegistry


ensure_scripts_importable()
import program_definitions  # noqa: E402
import program_legacy_compat  # noqa: E402


class FrozenLegacyCompatParityTests(unittest.TestCase):
    def test_frozen_payload_is_explicitly_named(self):
        self.assertTrue(hasattr(program_legacy_compat, "FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY"))
        payload = program_legacy_compat.FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY
        self.assertEqual(payload["definition_version"], "legacy-compat")
        self.assertEqual(payload["program_type"], "software_delivery")

    def test_frozen_payload_matches_software_delivery_topology_before_release(self):
        """Legacy programs keep the 1.1.0 graph: 1.2.0 only adds release and rollback."""
        builtin = program_definitions.program_to_payload(
            program_definitions.load_program(programs_dir() / "software_delivery.json")
        )
        release_nodes = {"release", "rollback"}
        frozen_view = program_legacy_compat.legacy_compat_topology_view()
        builtin_view = {
            "startable_nodes": [node for node in builtin["startable_nodes"] if node not in release_nodes],
            "nodes": [
                {
                    "id": node["id"],
                    "default_workflow_type": node["default_workflow_type"],
                    "workflow_variants": list(node.get("workflow_variants") or []),
                    "required_artifacts": list(node.get("required_artifacts") or []),
                    "terminal": bool(node.get("terminal")) or node["id"] == "implement",
                }
                for node in builtin["nodes"]
                if node["id"] not in release_nodes
            ],
            "edges": [
                {
                    "source": edge["source"],
                    "target": edge["target"],
                    "mode": edge.get("mode"),
                    "confirm_required": bool(edge.get("confirm_required")),
                    "signals": list(edge.get("signals") or []),
                    "artifact_patch": dict(edge.get("artifact_patch") or {}),
                }
                for edge in builtin["edges"]
                if edge["target"] not in release_nodes
            ],
        }
        self.assertEqual(frozen_view, builtin_view)

    def test_delivery_program_no_longer_exports_live_topology_constants(self):
        import ts_harness.workflows.delivery_program as delivery_program

        for name in (
            "PHASE_ORDER",
            "NEXT_PHASES",
            "PHASE_SKELETON",
            "ENTRY_REQUIREMENTS",
        ):
            self.assertFalse(
                hasattr(delivery_program, name),
                msg=f"{name} must not remain a live source of truth",
            )

    def test_memory_gateway_uses_frozen_payload_not_program_phases(self):
        import importlib.util

        gateway_path = Path(__file__).resolve().parents[2] / "scripts" / "memory-gateway.py"
        spec = importlib.util.spec_from_file_location("memory_gateway_legacy", gateway_path)
        gateway = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(gateway)

        self.assertFalse(hasattr(gateway, "PROGRAM_PHASES"))
        self.assertFalse(hasattr(gateway, "PROGRAM_NEXT_PHASES"))
        snap = gateway._legacy_compatibility_topology()
        self.assertEqual(snap["definition_version"], "legacy-compat")
        self.assertEqual(
            [node["id"] for node in snap["nodes"]],
            ["prd", "jira", "work_orders", "implement"],
        )
        self.assertEqual(snap["nodes"][0]["default_workflow_type"], "prd_request")


class ProgramRegistryRestartTests(unittest.TestCase):
    def _managed_payload(self, *, version: str = "2.0.0") -> dict:
        return {
            "program_type": "custom_ops",
            "definition_version": version,
            "display_name": "Custom ops",
            "startable_nodes": ["intake"],
            "nodes": [
                {
                    "id": "intake",
                    "default_workflow_type": "prd_request",
                    "workflow_variants": [],
                    "required_artifacts": [],
                    "produces_artifacts": [],
                    "input_bindings": [],
                    "completion_adapter": "extract_prd_outputs",
                    "terminal": True,
                }
            ],
            "edges": [],
        }

    def test_restart_reloads_active_managed_program_definitions(self):
        from ts_harness.workflows.definition_paths import program_content_revision

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "active"
            history = root / "history" / "custom_ops"
            active.mkdir(parents=True)
            history.mkdir(parents=True)
            payload = self._managed_payload()
            revision = program_content_revision(payload)
            (active / "custom_ops.json").write_text(json.dumps(payload), encoding="utf-8")
            (history / f"2.0.0-{revision[:12]}.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            (active / "manifest.json").write_text(
                json.dumps(
                    {
                        "generation": 7,
                        "programs": {
                            "custom_ops": {
                                "definition_version": "2.0.0",
                                "revision": revision,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            first = ProgramDefinitionRegistry(store_root=root)
            self.assertEqual(first.load("custom_ops")["definition_version"], "2.0.0")
            self.assertEqual(first.generation, 7)

            # Simulate process restart with a fresh registry instance.
            restarted = ProgramDefinitionRegistry(store_root=root)
            loaded = restarted.load("custom_ops")
            self.assertEqual(loaded["definition_version"], "2.0.0")
            self.assertEqual(restarted.generation, 7)
            self.assertIn("custom_ops", restarted.list_types())

    def test_missing_or_mismatched_managed_history_fails_closed(self):
        from ts_harness.workflows.definition_paths import program_content_revision

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "active"
            history = root / "history" / "custom_ops"
            active.mkdir(parents=True)
            history.mkdir(parents=True)
            payload = self._managed_payload(version="3.0.0")
            revision = program_content_revision(payload)
            stale = self._managed_payload(version="stale")
            (active / "custom_ops.json").write_text(json.dumps(stale), encoding="utf-8")
            (active / "manifest.json").write_text(
                json.dumps(
                    {
                        "generation": 4,
                        "programs": {
                            "custom_ops": {
                                "definition_version": "3.0.0",
                                "revision": revision,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            registry = ProgramDefinitionRegistry(store_root=root)
            self.assertNotIn("custom_ops", registry.list_types())
            with self.assertRaises(FileNotFoundError):
                registry.load("custom_ops")


if __name__ == "__main__":
    unittest.main()
