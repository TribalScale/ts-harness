# flake8: noqa
import json
import tempfile
import unittest
from pathlib import Path


class WorkflowSkeletonTests(unittest.TestCase):
    def load_module(self):
        from scripts import workflow_definitions

        return workflow_definitions

    def write_skeleton(self, payload):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with tmp:
            json.dump(payload, tmp)
        return Path(tmp.name)

    def base_skeleton(self):
        return {
            "workflow_type": "prd_request",
            "definition_version": "1.0.0",
            "display_name": "PRD Request",
            "experimental": True,
            "on_workflow_complete": {"hook": "complete_program_phase"},
            "contracts": {
                "prd": {
                    "output_schema": {"type": "object"},
                    "required_artifacts": {"body": "/body"},
                    "semantic_validators": ["validate_work_order_artifacts"],
                    "outcome": {
                        "pointer": "/status",
                        "succeeded_values": ["approved"],
                        "blocked_values": ["rejected"],
                    },
                }
            },
            "stages": [
                {
                    "name": "delivery_planning",
                    "kind": "expand",
                    "agent_id": "pm-agent",
                    "allowed_agents": ["pm-agent", "dev-agent"],
                    "allowed_external_effects": ["jira_write"],
                    "plan_approval_required": True,
                    "input_map": {"summary": "$input.summary", "priority": "high"},
                    "on_complete": {"hook": "hydrate_input", "downstream": ["review"]},
                    "contract_ref": "prd",
                }
            ],
        }

    def test_load_skeleton_defaults_retry_policy_and_requires_version(self):
        workflow_definitions = self.load_module()
        skeleton = self.base_skeleton()
        path = self.write_skeleton(skeleton)

        loaded = workflow_definitions.load_skeleton(path)

        stage = loaded.stage("delivery_planning")
        self.assertEqual(loaded.definition_version, "1.0.0")
        self.assertEqual(stage.retry["max_retries"], 2)
        self.assertEqual(stage.retry["timeout_seconds"], 1800)

    def test_load_skeleton_payload_matches_file_load(self):
        workflow_definitions = self.load_module()
        skeleton = self.base_skeleton()
        path = self.write_skeleton(skeleton)

        from_file = workflow_definitions.load_skeleton(path)
        from_payload = workflow_definitions.load_skeleton_payload(skeleton)

        self.assertEqual(from_payload, from_file)

    def test_skeleton_to_payload_round_trip_preserves_properties(self):
        workflow_definitions = self.load_module()
        loaded = workflow_definitions.load_skeleton_payload(self.base_skeleton())

        payload = workflow_definitions.skeleton_to_payload(loaded)
        reloaded = workflow_definitions.load_skeleton_payload(payload)

        self.assertEqual(reloaded, loaded)
        self.assertTrue(payload["experimental"])
        self.assertEqual(payload["on_workflow_complete"], {"hook": "complete_program_phase"})
        self.assertEqual(payload["stages"][0]["contract_ref"], "prd")

    def test_unknown_top_level_and_stage_fields_raise(self):
        workflow_definitions = self.load_module()
        top_level = self.base_skeleton()
        top_level["unexpected"] = True
        with self.assertRaises(ValueError):
            workflow_definitions.load_skeleton_payload(top_level)

        stage_level = self.base_skeleton()
        stage_level["stages"][0]["unexpected"] = True
        with self.assertRaises(ValueError):
            workflow_definitions.load_skeleton_payload(stage_level)

    def test_canonical_definition_json_is_deterministic(self):
        workflow_definitions = self.load_module()
        left = {
            "definition_version": "1.0.0",
            "workflow_type": "deterministic",
            "stages": [{"agent_id": "a", "kind": "task", "name": "start"}],
        }
        right = {
            "stages": [{"kind": "task", "name": "start", "agent_id": "a"}],
            "workflow_type": "deterministic",
            "definition_version": "1.0.0",
        }

        self.assertEqual(
            workflow_definitions.canonical_definition_json(left),
            workflow_definitions.canonical_definition_json(right),
        )
        self.assertTrue(workflow_definitions.canonical_definition_json(left).endswith("\n"))

    def test_expand_stage_cannot_require_task_approval(self):
        workflow_definitions = self.load_module()
        skeleton = self.base_skeleton()
        skeleton["stages"][0]["approval_required"] = True
        path = self.write_skeleton(skeleton)

        with self.assertRaises(ValueError):
            workflow_definitions.load_skeleton(path)

    def test_validate_proposed_tasks_rejects_disallowed_agent_and_effect(self):
        workflow_definitions = self.load_module()
        loaded = workflow_definitions.load_skeleton(self.write_skeleton(self.base_skeleton()))
        stage = loaded.stage("delivery_planning")

        with self.assertRaises(ValueError):
            workflow_definitions.validate_proposed_tasks(
                loaded,
                stage,
                [{"step_name": "deploy", "agent_id": "ops-agent", "input": {}}],
                existing_step_names=set(),
            )

        with self.assertRaises(ValueError):
            workflow_definitions.validate_proposed_tasks(
                loaded,
                stage,
                [
                    {
                        "step_name": "jira",
                        "agent_id": "pm-agent",
                        "input": {},
                        "external_effects": ["prod_deploy"],
                    }
                ],
                existing_step_names=set(),
            )

    def test_validate_proposed_tasks_accepts_policy_compliant_tasks(self):
        workflow_definitions = self.load_module()
        loaded = workflow_definitions.load_skeleton(self.write_skeleton(self.base_skeleton()))
        stage = loaded.stage("delivery_planning")

        result = workflow_definitions.validate_proposed_tasks(
            loaded,
            stage,
            [
                {
                    "step_name": "write_jira_story",
                    "agent_id": "pm-agent",
                    "input": {"story": "As a user..."},
                    "external_effects": ["jira_write"],
                }
            ],
            existing_step_names=set(),
        )

        self.assertEqual(result[0]["step_name"], "write_jira_story")
        self.assertEqual(result[0]["max_retries"], 2)

    def test_validate_proposed_tasks_stamps_version_without_contracts(self):
        workflow_definitions = self.load_module()
        payload = self.base_skeleton()
        payload["definition_version"] = "2026.08"
        payload.pop("contracts")
        payload["stages"][0].pop("contract_ref")
        loaded = workflow_definitions.load_skeleton(self.write_skeleton(payload))
        stage = loaded.stage("delivery_planning")

        result = workflow_definitions.validate_proposed_tasks(
            loaded,
            stage,
            [{"step_name": "write_notes", "agent_id": "pm-agent", "input": {}}],
            existing_step_names=set(),
        )

        self.assertEqual(result[0]["definition_version"], "2026.08")


if __name__ == "__main__":
    unittest.main()
