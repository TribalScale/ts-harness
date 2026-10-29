# flake8: noqa
"""Tests for program definition loading and built-in software_delivery parity."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.delivery_program import resolve_workflow_type

ensure_scripts_importable()
import program_legacy_compat  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
BUILTIN_PATH = ROOT / "config" / "programs" / "software_delivery.json"
SCHEMA_PATH = ROOT / "config" / "programs" / "schema.json"


class ProgramDefinitionLoaderTests(unittest.TestCase):
    def load_module(self):
        import program_definitions

        return program_definitions

    def minimal_program(self):
        return {
            "program_type": "example_program",
            "definition_version": "1.0.0",
            "display_name": "Example",
            "startable_nodes": ["start"],
            "nodes": [
                {
                    "id": "start",
                    "default_workflow_type": "prd_request",
                    "workflow_variants": [],
                    "required_artifacts": [],
                    "produces_artifacts": ["approved_prd"],
                    "input_bindings": [
                        {"kind": "program_summary", "target": "summary"},
                    ],
                    "completion_adapter": "extract_prd_outputs",
                    "terminal": False,
                },
                {
                    "id": "done",
                    "default_workflow_type": "deploy_release",
                    "workflow_variants": [],
                    "required_artifacts": ["pr_url"],
                    "produces_artifacts": [],
                    "input_bindings": [
                        {"kind": "artifact", "name": "pr_url", "target": "pr_url"},
                    ],
                    "completion_adapter": "extract_deploy_outputs",
                    "terminal": True,
                },
            ],
            "edges": [
                {
                    "source": "start",
                    "target": "done",
                    "mode": "manual",
                    "confirm_required": False,
                    "signals": [],
                    "artifact_patch": {},
                }
            ],
        }

    def test_load_program_payload_round_trip(self):
        program_definitions = self.load_module()
        loaded = program_definitions.load_program_payload(self.minimal_program())
        payload = program_definitions.program_to_payload(loaded)
        reloaded = program_definitions.load_program_payload(payload)
        self.assertEqual(reloaded, loaded)
        self.assertEqual(loaded.program_type, "example_program")
        self.assertEqual(program_definitions.required_permission_for(loaded, "start"), "start_program")
        self.assertEqual(loaded.nodes[0].id, "start")
        self.assertEqual(loaded.edges[0].mode, "manual")

    def test_node_permission_overrides_the_program_permission(self):
        program_definitions = self.load_module()
        payload = self.minimal_program()
        payload["required_permission"] = "start_program"
        payload["nodes"][1]["required_permission"] = "start_delivery"
        loaded = program_definitions.load_program_payload(payload)
        self.assertEqual(program_definitions.required_permission_for(loaded, "start"), "start_program")
        self.assertEqual(program_definitions.required_permission_for(loaded, "done"), "start_delivery")
        self.assertEqual(program_definitions.required_permission_for(loaded, None), "start_program")
        self.assertEqual(program_definitions.required_permission_for(None, "done"), "start_program")

    def test_bad_permission_name_is_rejected(self):
        program_definitions = self.load_module()
        payload = self.minimal_program()
        payload["required_permission"] = "Start-Delivery"
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(payload)

    def test_unknown_keys_and_duplicate_ids_rejected(self):
        program_definitions = self.load_module()
        top = self.minimal_program()
        top["unexpected"] = True
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(top)

        node_unknown = self.minimal_program()
        node_unknown["nodes"][0]["unexpected"] = True
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(node_unknown)

        edge_unknown = self.minimal_program()
        edge_unknown["edges"][0]["unexpected"] = True
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(edge_unknown)

        dup_nodes = self.minimal_program()
        dup_nodes["nodes"].append(copy.deepcopy(dup_nodes["nodes"][0]))
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(dup_nodes)

    def test_unsupported_transition_mode_rejected(self):
        program_definitions = self.load_module()
        payload = self.minimal_program()
        payload["edges"][0]["mode"] = "conditional"
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(payload)

    def test_malformed_variants_and_adapters_rejected(self):
        program_definitions = self.load_module()
        bad_variant = self.minimal_program()
        bad_variant["nodes"][0]["workflow_variants"] = [
            {"workflow_type": "implement_work_orders"}
        ]
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(bad_variant)

        empty_when = self.minimal_program()
        empty_when["nodes"][0]["workflow_variants"] = [
            {"workflow_type": "implement_work_orders", "when": {}}
        ]
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(empty_when)

        bad_adapter = self.minimal_program()
        bad_adapter["nodes"][0]["completion_adapter"] = "not_a_real_adapter"
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(bad_adapter)

        bad_binding = self.minimal_program()
        bad_binding["nodes"][0]["input_bindings"] = [{"kind": "expression", "target": "x"}]
        with self.assertRaises(ValueError):
            program_definitions.load_program_payload(bad_binding)

    def test_canonical_program_json_is_deterministic(self):
        program_definitions = self.load_module()
        left = self.minimal_program()
        right = copy.deepcopy(left)
        right["nodes"] = list(reversed(right["nodes"]))
        right["display_name"] = left["display_name"]
        # Restore node order dependency: canonicalization should normalize key order,
        # not invent graph order. Keep same node order, shuffle only key order.
        right = {
            "edges": left["edges"],
            "nodes": left["nodes"],
            "startable_nodes": left["startable_nodes"],
            "display_name": left["display_name"],
            "definition_version": left["definition_version"],
            "program_type": left["program_type"],
        }
        self.assertEqual(
            program_definitions.canonical_program_json(left),
            program_definitions.canonical_program_json(right),
        )
        self.assertTrue(program_definitions.canonical_program_json(left).endswith("\n"))

    def test_schema_file_exists_and_is_strict(self):
        self.assertTrue(SCHEMA_PATH.is_file())
        schema = json.loads(SCHEMA_PATH.read_text())
        self.assertEqual(schema.get("additionalProperties"), False)
        self.assertIn("nodes", schema["properties"])
        self.assertIn("edges", schema["properties"])
        node_schema = schema["definitions"]["node"]
        self.assertEqual(node_schema.get("additionalProperties"), False)
        edge_schema = schema["definitions"]["edge"]
        self.assertEqual(edge_schema.get("additionalProperties"), False)


class SoftwareDeliveryBuiltinParityTests(unittest.TestCase):
    def load_builtin(self):
        import program_definitions

        payload = json.loads(BUILTIN_PATH.read_text())
        return program_definitions.load_program_payload(payload), payload

    def test_builtin_file_loads(self):
        definition, payload = self.load_builtin()
        self.assertEqual(definition.program_type, "software_delivery")
        self.assertEqual(definition.definition_version, "1.3.0")
        self.assertTrue(definition.display_name)
        self.assertEqual(payload["program_type"], "software_delivery")

    def test_startable_nodes_match_phase_order(self):
        definition, _payload = self.load_builtin()
        frozen = program_legacy_compat.frozen_legacy_compatibility_topology()
        self.assertEqual(
            tuple(definition.startable_nodes),
            (*frozen["startable_nodes"], "rollback"),
        )

    def test_nodes_match_phase_skeleton_and_entry_requirements(self):
        definition, _payload = self.load_builtin()
        frozen = program_legacy_compat.frozen_legacy_compatibility_topology()
        by_id = {node.id: node for node in definition.nodes}
        frozen_by_id = {node["id"]: node for node in frozen["nodes"]}
        self.assertEqual(set(by_id), set(frozen_by_id) | {"release", "rollback"})
        for phase, node in frozen_by_id.items():
            self.assertEqual(by_id[phase].default_workflow_type, node["default_workflow_type"])
            self.assertEqual(
                by_id[phase].required_artifacts,
                list(node.get("required_artifacts") or []),
            )
        self.assertNotIn("deploy", by_id)
        for phase in ("prd", "jira", "work_orders", "implement"):
            self.assertFalse(by_id[phase].terminal)
        self.assertTrue(by_id["release"].terminal)
        self.assertTrue(by_id["release"].await_children)
        self.assertEqual(by_id["release"].default_workflow_type, "deploy_release")
        self.assertEqual(by_id["release"].required_artifacts, ["merge_sha"])
        self.assertEqual(by_id["rollback"].default_workflow_type, "rollback_release")

    def test_edges_match_next_phases_and_prd_fork(self):
        definition, _payload = self.load_builtin()
        frozen = program_legacy_compat.frozen_legacy_compatibility_topology()
        successors: dict[str, set[str]] = {}
        for edge in definition.edges:
            if edge.target in {"release", "rollback"}:
                continue
            successors.setdefault(edge.source, set()).add(edge.target)
        frozen_successors: dict[str, set[str]] = {}
        for edge in frozen["edges"]:
            frozen_successors.setdefault(edge["source"], set()).add(edge["target"])
        self.assertEqual(successors, frozen_successors)

        prd_edges = [edge for edge in definition.edges if edge.source == "prd"]
        self.assertEqual({edge.target for edge in prd_edges}, {"jira", "work_orders"})
        for edge in prd_edges:
            self.assertEqual(edge.mode, "manual")
            self.assertTrue(edge.confirm_required)
            self.assertTrue(edge.signals)

        work_orders_edges = [edge for edge in definition.edges if edge.source == "work_orders"]
        self.assertEqual(len(work_orders_edges), 1)
        self.assertEqual(work_orders_edges[0].mode, "automatic")

        implement_edges = [edge for edge in definition.edges if edge.source == "implement"]
        self.assertEqual([(edge.target, edge.mode) for edge in implement_edges], [("release", "automatic")])

    def test_implement_variants_match_resolve_workflow_type(self):
        definition, _payload = self.load_builtin()
        implement = next(node for node in definition.nodes if node.id == "implement")
        self.assertEqual(implement.default_workflow_type, "implement_delivery")
        self.assertGreaterEqual(len(implement.workflow_variants), 1)

        def resolve_from_definition(artifacts: dict) -> str:
            for variant in implement.workflow_variants:
                if all(artifacts.get(key) == value for key, value in variant.when.items()):
                    return variant.workflow_type
            return implement.default_workflow_type

        cases = [
            {},
            {"planning_track": "work_orders"},
            {"work_orders_handoff": True},
            {"planning_track": "jira"},
            {"planning_track": "work_orders", "work_orders_handoff": False},
        ]
        for artifacts in cases:
            self.assertEqual(
                resolve_from_definition(artifacts),
                resolve_workflow_type("implement", artifacts),
                msg=artifacts,
            )

    def test_prd_bridge_signals_encoded_on_edges(self):
        definition, _payload = self.load_builtin()
        jira_edge = next(
            edge for edge in definition.edges if edge.source == "prd" and edge.target == "jira"
        )
        work_orders_edge = next(
            edge
            for edge in definition.edges
            if edge.source == "prd" and edge.target == "work_orders"
        )
        for signal in ("create jira", "file tickets", "jira"):
            self.assertIn(signal, jira_edge.signals)
        for signal in ("work orders", "plan work orders", "start work orders"):
            self.assertIn(signal, work_orders_edge.signals)

    def test_input_bindings_cover_static_stage_inputs(self):
        definition, _payload = self.load_builtin()
        by_id = {node.id: node for node in definition.nodes}

        def binding_kinds(node_id: str) -> set[str]:
            return {binding["kind"] for binding in by_id[node_id].input_bindings}

        def has_static(node_id: str, target: str, value) -> bool:
            return any(
                binding.get("kind") == "static"
                and binding.get("target") == target
                and binding.get("value") == value
                for binding in by_id[node_id].input_bindings
            )

        self.assertIn("program_summary", binding_kinds("prd"))
        self.assertIn("transcript_context", binding_kinds("prd"))
        self.assertIn("artifact", binding_kinds("jira"))
        self.assertTrue(has_static("work_orders", "stage", "refinery"))
        self.assertIn("artifact", binding_kinds("implement"))

    def test_completion_adapters_are_registered_builtins(self):
        import program_definitions

        definition, _payload = self.load_builtin()
        for node in definition.nodes:
            self.assertIn(node.completion_adapter, program_definitions.KNOWN_COMPLETION_ADAPTERS)

    def test_builtin_round_trip_canonical(self):
        import program_definitions

        raw = json.loads(BUILTIN_PATH.read_text())
        canonical = program_definitions.canonical_program_json(raw)
        reloaded = program_definitions.load_program_payload(json.loads(canonical))
        again = program_definitions.canonical_program_json(
            program_definitions.program_to_payload(reloaded)
        )
        self.assertEqual(canonical, again)


if __name__ == "__main__":
    unittest.main()
