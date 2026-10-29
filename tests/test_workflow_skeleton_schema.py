"""Validate workflow skeleton JSON files against schema."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    import jsonschema
except ImportError:
    jsonschema = None

import workflow_definitions


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / "config" / "workflows"
SCHEMA_PATH = WORKFLOW_DIR / "schema.json"


class WorkflowSkeletonSchemaTests(unittest.TestCase):
    def test_all_skeletons_load(self):
        for path in sorted(WORKFLOW_DIR.glob("*.json")):
            if path.name == "schema.json":
                continue
            skeleton = workflow_definitions.load_skeleton(path)
            self.assertTrue(skeleton.workflow_type)
            self.assertTrue(skeleton.stages)

    @unittest.skipUnless(jsonschema is not None, "jsonschema not installed")
    def test_schema_validation(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        for path in sorted(WORKFLOW_DIR.glob("*.json")):
            if path.name == "schema.json":
                continue
            data = json.loads(path.read_text())
            jsonschema.validate(data, schema)

    @unittest.skipUnless(jsonschema is not None, "jsonschema not installed")
    def test_schema_defines_contract_object(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        self.assertIn("contracts", schema["properties"])
        self.assertIn("contract", schema["definitions"])
        contract_props = schema["definitions"]["contract"]["properties"]
        for key in ("output_schema", "required_artifacts", "semantic_validators", "outcome"):
            self.assertIn(key, contract_props)
        stage_props = schema["definitions"]["stage"]["properties"]
        for key in ("contract_ref", "allowed_contracts", "output_schema"):
            self.assertIn(key, stage_props)

    @unittest.skipUnless(jsonschema is not None, "jsonschema not installed")
    def test_schema_accepts_valid_contracts_map(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        data = {
            "workflow_type": "x",
            "definition_version": "1.0.0",
            "contracts": {
                "c1": {
                    "output_schema": {"type": "object"},
                    "required_artifacts": {"body": "/artifacts/body"},
                    "semantic_validators": ["validate_work_order_artifacts"],
                    "outcome": {
                        "pointer": "/status",
                        "succeeded_values": ["success"],
                        "blocked_values": ["failed"],
                    },
                }
            },
            "stages": [
                {"name": "s1", "kind": "task", "agent_id": "a", "contract_ref": "c1"}
            ],
        }
        jsonschema.validate(data, schema)

    @unittest.skipUnless(jsonschema is not None, "jsonschema not installed")
    def test_schema_rejects_contract_with_unknown_keys(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        data = {
            "workflow_type": "x",
            "definition_version": "1.0.0",
            "contracts": {"c1": {"bogus_key": 1}},
            "stages": [{"name": "s1", "kind": "task", "agent_id": "a"}],
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(data, schema)


class WorkflowContractInventoryTests(unittest.TestCase):
    """Every targeted active skeleton stage must resolve a contract (Phase 1)."""

    TARGETS = {
        "prd_request": ["intake", "draft_prd"],
        "work_orders_from_prd": [
            "refine_requirements",
            "design_blueprints",
            "plan_work_orders",
            "prepare_work_order_delivery",
        ],
        "deliver_work_order": ["deliver_work_order"],
        "jira_from_prd": ["review_prd", "plan_stories", "file_jira"],
        "rfp_request": ["intake", "draft_rfp"],
    }

    def test_targeted_stages_resolve_contracts(self):
        for workflow_type, stage_names in self.TARGETS.items():
            skeleton = workflow_definitions.load_skeleton(
                WORKFLOW_DIR / f"{workflow_type}.json"
            )
            for stage_name in stage_names:
                with self.subTest(workflow=workflow_type, stage=stage_name):
                    contract = skeleton.resolve_stage_contract(stage_name)
                    self.assertIsNotNone(
                        contract,
                        f"{workflow_type}.{stage_name} must resolve a contract",
                    )

    def test_implement_delivery_expand_declares_allowed_contracts(self):
        skeleton = workflow_definitions.load_skeleton(
            WORKFLOW_DIR / "implement_delivery.json"
        )
        stage = skeleton.stage("implement_tickets")
        self.assertTrue(stage.allowed_contracts)
        for ref in stage.allowed_contracts:
            self.assertIn(ref, skeleton.contracts)


if __name__ == "__main__":
    unittest.main()
