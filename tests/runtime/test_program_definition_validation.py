from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.workflows.materialize import build_workflow_catalog
from ts_harness.workflows.program_adapters import (
    ProgramAdapterRegistry,
    build_default_adapter_registry,
)
from ts_harness.workflows.program_definition_validation import (
    validate_program_definition,
)


class ProgramDefinitionValidationTests(unittest.TestCase):
    def catalogs(self):
        return {
            "workflow_catalog": {
                "prd_request": {
                    "workflow_type": "prd_request",
                    "status": "built_in",
                    "available_versions": ["1.3.0"],
                    "resolvable": True,
                    "active_version": "1.3.0",
                    "contract_output_keys": {"questions", "prd_markdown", "result"},
                },
                "deploy_release": {
                    "workflow_type": "deploy_release",
                    "status": "built_in",
                    "available_versions": ["1.0.0"],
                    "resolvable": True,
                    "active_version": "1.0.0",
                    "contract_output_keys": {"merge_sha"},
                },
                "implement_delivery": {
                    "workflow_type": "implement_delivery",
                    "status": "built_in",
                    "available_versions": ["1.0.0"],
                    "resolvable": True,
                    "active_version": "1.0.0",
                    "contract_output_keys": {"pr_url"},
                },
                "implement_work_orders": {
                    "workflow_type": "implement_work_orders",
                    "status": "built_in",
                    "available_versions": ["1.0.0"],
                    "resolvable": True,
                    "active_version": "1.0.0",
                    "contract_output_keys": {"pr_url", "work_order_id"},
                },
            },
            "adapter_names": {
                "extract_prd_outputs",
                "extract_jira_outputs",
                "extract_work_orders_outputs",
                "extract_implement_outputs",
                "extract_deploy_outputs",
                "extract_declared_outputs",
            },
            "built_in_types": {"software_delivery"},
        }

    def valid_payload(self):
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
                    "produces_artifacts": ["approved_prd", "planning_track"],
                    "input_bindings": [
                        {"kind": "program_summary", "target": "summary"},
                    ],
                    "completion_adapter": "extract_prd_outputs",
                    "terminal": False,
                },
                {
                    "id": "middle",
                    "default_workflow_type": "implement_delivery",
                    "workflow_variants": [
                        {
                            "workflow_type": "implement_work_orders",
                            "when": {"planning_track": "work_orders"},
                        }
                    ],
                    "required_artifacts": ["approved_prd"],
                    "produces_artifacts": ["pr_url"],
                    "input_bindings": [
                        {
                            "kind": "artifact",
                            "name": "approved_prd",
                            "target": "approved_prd",
                        }
                    ],
                    "completion_adapter": "extract_implement_outputs",
                    "terminal": False,
                },
                {
                    "id": "done",
                    "default_workflow_type": "deploy_release",
                    "workflow_variants": [],
                    "required_artifacts": ["pr_url"],
                    "produces_artifacts": ["merge_sha"],
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
                    "target": "middle",
                    "mode": "manual",
                    "confirm_required": False,
                    "signals": [],
                    "artifact_patch": {},
                },
                {
                    "source": "middle",
                    "target": "done",
                    "mode": "manual",
                    "confirm_required": False,
                    "signals": [],
                    "artifact_patch": {},
                },
            ],
        }

    def validate(self, payload, **overrides):
        catalogs = self.catalogs()
        catalogs.update(overrides)
        return validate_program_definition(payload, **catalogs)

    def issue_codes(self, issues):
        return {issue.code for issue in issues}

    def test_valid_definition_returns_program_without_errors(self):
        definition, issues = self.validate(self.valid_payload())

        self.assertIsNotNone(definition)
        errors = [issue for issue in issues if issue.severity == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(definition.program_type, "example_program")
        # planning_track is listed on prd but only extract_jira/work_orders emit it
        self.assertIn("unconfirmed_output_artifact", self.issue_codes(issues))
        messages = [issue.message for issue in issues if issue.code == "unconfirmed_output_artifact"]
        self.assertTrue(any("planning_track" in message for message in messages))
        self.assertFalse(any("approved_prd" in message for message in messages))

    def test_unknown_required_permission_fails_until_a_bundle_declares_it(self):
        from types import SimpleNamespace

        payload = self.valid_payload()
        payload["nodes"][0]["required_permission"] = "start_delivery"
        _, issues = self.validate(payload)
        self.assertIn("unknown_permission", self.issue_codes(issues))
        config = SimpleNamespace(
            identity=SimpleNamespace(role_bundles={"delivery_lead": ["view", "start_delivery"]})
        )
        _, allowed = self.validate(payload, config=config)
        self.assertNotIn("unknown_permission", self.issue_codes(allowed))

    def test_software_delivery_produces_are_confirmed_by_adapters(self):
        root = Path(__file__).resolve().parents[2]
        payload = json.loads(
            (root / "config" / "programs" / "software_delivery.json").read_text()
        )
        payload = copy.deepcopy(payload)
        payload["program_type"] = "software_delivery_ui"
        catalog = build_workflow_catalog()
        definition, issues = self.validate(
            payload,
            workflow_catalog=catalog,
        )
        self.assertIsNotNone(definition)
        errors = [issue for issue in issues if issue.severity == "error"]
        self.assertEqual(errors, [])
        self.assertNotIn("unconfirmed_output_artifact", self.issue_codes(issues))

    def test_declared_outputs_adapter_trusts_produces_list(self):
        payload = self.valid_payload()
        payload["nodes"] = [
            {
                "id": "start",
                "default_workflow_type": "prd_request",
                "workflow_variants": [],
                "required_artifacts": [],
                "produces_artifacts": ["approved_rfp", "custom_key"],
                "input_bindings": [
                    {"kind": "program_summary", "target": "summary"},
                ],
                "completion_adapter": "extract_declared_outputs",
                "terminal": True,
            }
        ]
        payload["edges"] = []
        definition, issues = self.validate(payload)
        self.assertIsNotNone(definition)
        self.assertNotIn("unconfirmed_output_artifact", self.issue_codes(issues))

    def test_reports_schema_and_builtin_collision(self):
        payload = self.valid_payload()
        payload["unexpected"] = True
        payload["program_type"] = "software_delivery"

        _definition, issues = self.validate(payload)

        self.assertIn("schema_load_error", self.issue_codes(issues))
        self.assertIn("built_in_type_collision", self.issue_codes(issues))

    def test_reports_graph_topology_errors(self):
        payload = self.valid_payload()
        payload["nodes"].append(
            {
                "id": "lonely",
                "default_workflow_type": "prd_request",
                "workflow_variants": [],
                "required_artifacts": [],
                "produces_artifacts": [],
                "input_bindings": [
                    {"kind": "program_summary", "target": "summary"},
                ],
                "completion_adapter": "extract_prd_outputs",
                "terminal": False,
            }
        )
        payload["edges"] = [
            {
                "source": "start",
                "target": "start",
                "mode": "manual",
                "confirm_required": False,
                "signals": [],
                "artifact_patch": {},
            },
            {
                "source": "middle",
                "target": "done",
                "mode": "manual",
                "confirm_required": False,
                "signals": [],
                "artifact_patch": {},
            },
            {
                "source": "done",
                "target": "middle",
                "mode": "manual",
                "confirm_required": False,
                "signals": [],
                "artifact_patch": {},
            },
        ]
        payload["nodes"][2]["terminal"] = True

        _definition, issues = self.validate(payload)

        codes = self.issue_codes(issues)
        self.assertIn("self_edge", codes)
        self.assertIn("cycle", codes)
        self.assertIn("unreachable_node", codes)
        self.assertIn("missing_outgoing_edge", codes)
        self.assertIn("terminal_has_outgoing_edge", codes)

    def test_reports_unresolved_and_unresolvable_workflows(self):
        payload = self.valid_payload()
        payload["nodes"][0]["default_workflow_type"] = "missing_workflow"
        payload["nodes"][1]["workflow_variants"] = [
            {
                "workflow_type": "implement_work_orders",
                "when": {"planning_track": "work_orders"},
            }
        ]
        catalog = self.catalogs()["workflow_catalog"]
        catalog = copy.deepcopy(catalog)
        catalog["implement_work_orders"]["resolvable"] = False

        _definition, issues = self.validate(payload, workflow_catalog=catalog)

        codes = self.issue_codes(issues)
        self.assertIn("unresolved_workflow", codes)
        self.assertIn("unresolvable_workflow", codes)

    def test_reports_variant_condition_and_artifact_errors(self):
        payload = self.valid_payload()
        payload["nodes"][1]["workflow_variants"] = [
            {
                "workflow_type": "implement_work_orders",
                "when": {"planning_track": "work_orders"},
            },
            {
                "workflow_type": "implement_work_orders",
                "when": {"planning_track": "work_orders"},
            },
            {
                "workflow_type": "implement_work_orders",
                "when": {"unknown_artifact": True},
            },
        ]
        payload["nodes"][1]["required_artifacts"] = ["missing_upstream"]

        _definition, issues = self.validate(payload)

        codes = self.issue_codes(issues)
        self.assertIn("duplicate_variant_condition", codes)
        self.assertIn("unknown_variant_condition_artifact", codes)
        self.assertIn("missing_required_artifact", codes)

    def test_rejects_unknown_adapter_and_bad_primitives(self):
        payload = self.valid_payload()
        # Loader-known adapter that is absent from the narrower server registry.
        payload["nodes"][0]["input_bindings"] = [
            {"kind": "program_summary", "target": "summary"},
            {"kind": "static", "target": "flag", "value": {"nested": True}},
        ]
        payload["edges"][0]["artifact_patch"] = {"planning_track": ["not", "primitive"]}

        _definition, issues = self.validate(
            payload,
            adapter_names={"extract_implement_outputs", "extract_deploy_outputs"},
        )

        codes = self.issue_codes(issues)
        self.assertIn("unknown_completion_adapter", codes)
        self.assertIn("unsupported_artifact_patch", codes)
        self.assertIn("unsupported_input_binding", codes)

    def test_software_delivery_builtin_validates_with_allow_flag(self):
        root = Path(__file__).resolve().parents[2]
        payload = json.loads(
            (root / "config" / "programs" / "software_delivery.json").read_text()
        )
        catalog = build_workflow_catalog()
        registry = build_default_adapter_registry()

        definition, issues = validate_program_definition(
            payload,
            workflow_catalog=catalog,
            adapter_names=set(registry.names()),
            built_in_types={"software_delivery"},
            allow_builtin_type=True,
        )

        self.assertIsNotNone(definition)
        errors = [issue for issue in issues if issue.severity == "error"]
        self.assertEqual(errors, [])


class WorkflowCatalogMaterializeTests(unittest.TestCase):
    def test_build_workflow_catalog_includes_versions_status_resolvability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = root / "builtin"
            managed = root / "managed"
            store = root / "store"
            builtin.mkdir()
            managed.mkdir()
            (store / "active").mkdir(parents=True)
            (store / "history" / "custom_flow").mkdir(parents=True)

            builtin_payload = {
                "workflow_type": "prd_request",
                "definition_version": "1.0.0",
                "stages": [{"name": "start", "kind": "task", "agent_id": "a"}],
            }
            (builtin / "prd_request.json").write_text(
                json.dumps(builtin_payload),
                encoding="utf-8",
            )

            managed_payload = {
                "workflow_type": "custom_flow",
                "definition_version": "2.0.0",
                "stages": [{"name": "start", "kind": "task", "agent_id": "a"}],
            }
            (managed / "custom_flow.json").write_text(
                json.dumps(managed_payload),
                encoding="utf-8",
            )
            history_payload = {
                "workflow_type": "custom_flow",
                "definition_version": "1.5.0",
                "stages": [{"name": "start", "kind": "task", "agent_id": "a"}],
            }
            (store / "history" / "custom_flow" / "1.5.0-abc.json").write_text(
                json.dumps(history_payload),
                encoding="utf-8",
            )
            # History is discoverable even without a matching active revision pin.
            (store / "active" / "manifest.json").write_text(
                json.dumps({"workflows": {}}),
                encoding="utf-8",
            )

            import os

            previous_managed = os.environ.get("TS_HARNESS_MANAGED_WORKFLOWS_DIR")
            previous_store = os.environ.get("TS_HARNESS_WORKFLOW_DEFINITION_STORE")
            try:
                os.environ["TS_HARNESS_MANAGED_WORKFLOWS_DIR"] = str(managed)
                os.environ["TS_HARNESS_WORKFLOW_DEFINITION_STORE"] = str(store)
                catalog = build_workflow_catalog(skeleton_dir=builtin)
            finally:
                if previous_managed is None:
                    os.environ.pop("TS_HARNESS_MANAGED_WORKFLOWS_DIR", None)
                else:
                    os.environ["TS_HARNESS_MANAGED_WORKFLOWS_DIR"] = previous_managed
                if previous_store is None:
                    os.environ.pop("TS_HARNESS_WORKFLOW_DEFINITION_STORE", None)
                else:
                    os.environ["TS_HARNESS_WORKFLOW_DEFINITION_STORE"] = previous_store

        self.assertIn("prd_request", catalog)
        self.assertTrue(catalog["prd_request"]["resolvable"])
        self.assertEqual(catalog["prd_request"]["status"], "built_in")
        self.assertIn("1.0.0", catalog["prd_request"]["available_versions"])

        self.assertIn("custom_flow", catalog)
        self.assertTrue(catalog["custom_flow"]["resolvable"])
        self.assertEqual(catalog["custom_flow"]["status"], "active")
        self.assertIn("2.0.0", catalog["custom_flow"]["available_versions"])
        self.assertIn("1.5.0", catalog["custom_flow"]["available_versions"])


class ProgramAdapterRegistryTests(unittest.TestCase):
    def test_default_registry_exposes_builtin_adapters(self):
        registry = build_default_adapter_registry()
        names = set(registry.names())
        self.assertEqual(
            names,
            {
                "extract_prd_outputs",
                "extract_jira_outputs",
                "extract_work_orders_outputs",
                "extract_implement_outputs",
                "extract_deploy_outputs",
                "extract_declared_outputs",
                "extract_file_jira_outputs",
                "extract_pick_ticket_outputs",
            },
        )

    def test_prd_adapter_builds_inputs_and_extracts_outputs(self):
        registry = build_default_adapter_registry()
        inputs = registry.build_inputs(
            "extract_prd_outputs",
            summary="Ship it",
            artifacts={},
            transcript_messages=[{"role": "user", "content": "hi"}],
        )
        self.assertEqual(inputs["summary"], "Ship it")
        self.assertEqual(inputs["context"]["transcript"][-1]["content"], "hi")

        outputs = registry.extract_outputs(
            "extract_prd_outputs",
            workflow_type="prd_request",
            tasks=[
                {
                    "status": "succeeded",
                    "step_name": "approve_prd",
                    "output": {"approved_prd": {"title": "PRD"}},
                }
            ],
            outputs={"approved_prd": {"title": "PRD"}},
        )
        self.assertEqual(outputs["approved_prd"]["title"], "PRD")

    def test_registry_rejects_unknown_adapter(self):
        registry = ProgramAdapterRegistry()
        with self.assertRaises(KeyError):
            registry.build_inputs("missing", summary="x", artifacts={})


if __name__ == "__main__":
    unittest.main()
