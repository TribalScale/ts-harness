"""Tests for program definition registry hot-reload behavior."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.workflows.program_registry import ProgramDefinitionRegistry


class ProgramDefinitionRegistryTests(unittest.TestCase):
    def test_loads_builtin_software_delivery(self):
        registry = ProgramDefinitionRegistry()
        loaded = registry.load("software_delivery")
        self.assertEqual(loaded["program_type"], "software_delivery")
        self.assertEqual(loaded["definition_version"], "1.2.0")
        self.assertIn("software_delivery", registry.list_types())
        self.assertIn("content_to_jira_work_orders", registry.list_types())
        self.assertIn("bug_to_jira_work_orders", registry.list_types())
        self.assertIn("jira_to_pr", registry.list_types())

    def test_reload_failure_keeps_last_good(self):
        registry = ProgramDefinitionRegistry()
        first = registry.load("software_delivery")
        generation = registry.generation
        with patch.object(
            registry,
            "_read_manifest_generation",
            side_effect=RuntimeError("boom"),
        ):
            ok = registry.reload_if_changed()
        self.assertFalse(ok)
        self.assertEqual(registry.generation, generation)
        self.assertEqual(
            registry.load("software_delivery")["definition_version"],
            first["definition_version"],
        )

    def test_blocks_unavailable_managed_definition(self):
        registry = ProgramDefinitionRegistry()
        with self.assertRaises(FileNotFoundError):
            registry.load("missing_managed_program")

    def test_reload_picks_up_active_managed_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "active"
            active.mkdir(parents=True)
            payload = {
                "program_type": "custom_ops",
                "definition_version": "9.0.0",
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
            (active / "custom_ops.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            from ts_harness.workflows.definition_paths import program_content_revision

            revision = program_content_revision(payload)
            (active / "manifest.json").write_text(
                json.dumps(
                    {
                        "generation": 3,
                        "programs": {
                            "custom_ops": {
                                "definition_version": "9.0.0",
                                "revision": revision,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            registry = ProgramDefinitionRegistry(store_root=root)
            loaded = registry.load("custom_ops")
            self.assertEqual(loaded["definition_version"], "9.0.0")
            self.assertEqual(registry.generation, 3)


if __name__ == "__main__":
    unittest.main()
