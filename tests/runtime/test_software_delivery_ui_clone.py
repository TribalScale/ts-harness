"""Clone software_delivery into unique managed types (activation + reentry proof)."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.workflows.definition_store import WorkflowDefinitionStore
from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.hooks import build_default_registry, complete_program_phase
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.program_definition_store import ProgramDefinitionStore
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.delivery_program import DeliveryProgramService

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "config" / "workflows"
PROGRAMS = ROOT / "config" / "programs"


CLONE_WORKFLOWS = [
    "prd_request",
    "jira_from_prd",
    "work_orders_from_prd",
    "implement_work_orders",
    "implement_delivery",
]


def _agent_catalog() -> set[str]:
    return {
        "pm-agent",
        "dev-agent",
        "qa-agent",
        "orchestrator",
        "ops-agent",
        "sales-agent",
    }


def _skill_catalog(payloads: list[dict] | None = None) -> set[str]:
    skills = {
        "clarify-requirements",
        "draft-prd",
        "verified-merge-governed",
        "governed-qa-gate",
        "prd-to-work-orders-governed",
        "source-work-order-governed",
        "delivery-planning",
        "memory-api",
    }
    for payload in payloads or []:
        for stage in payload.get("stages") or []:
            for skill in stage.get("skill_hints") or []:
                skills.add(str(skill))
    return skills


class SoftwareDeliveryUiCloneTests(unittest.TestCase):
    def test_cloned_skeletons_validate_and_activate(self):
        hooks = set(build_default_registry().names())
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowDefinitionStore(Path(tmp) / "workflows")
            payloads = []
            for name in CLONE_WORKFLOWS:
                payload = copy.deepcopy(json.loads((WORKFLOWS / f"{name}.json").read_text()))
                payload["workflow_type"] = f"{name}_ui"
                payload["definition_version"] = "1.0.0"
                payloads.append(payload)
            skills = _skill_catalog(payloads)
            for name, payload in zip(CLONE_WORKFLOWS, payloads):
                skeleton, issues = validate_workflow_definition(
                    payload,
                    hook_names=hooks,
                    agent_ids=_agent_catalog(),
                    skill_ids=skills,
                    built_in_types={name},
                )
                errors = [issue for issue in issues if issue.severity == "error"]
                self.assertIsNotNone(skeleton)
                self.assertEqual(errors, [], errors)
                draft = store.create_draft(payload, actor_id="admin")
                activated = store.activate(
                    draft["id"],
                    expected_revision=draft["revision"],
                    actor_id="admin",
                )
                self.assertEqual(activated["workflow_type"], f"{name}_ui")

    def test_cloned_program_keeps_extract_adapters_and_reentry(self):
        program = json.loads((PROGRAMS / "software_delivery.json").read_text())
        program = copy.deepcopy(program)
        program["program_type"] = "software_delivery_ui"
        program["definition_version"] = "1.0.0"
        program["display_name"] = "Software delivery UI clone"
        for node in program["nodes"]:
            node["default_workflow_type"] = f"{node['default_workflow_type']}_ui"
            variants = []
            for variant in node.get("workflow_variants") or []:
                variants.append(
                    {
                        **variant,
                        "workflow_type": f"{variant['workflow_type']}_ui",
                    }
                )
            node["workflow_variants"] = variants
        implement = next(node for node in program["nodes"] if node["id"] == "implement")
        self.assertEqual(implement["completion_adapter"], "extract_implement_outputs")
        self.assertIn("reentry", implement)
        self.assertTrue(implement.get("complete_channel_text"))

        with tempfile.TemporaryDirectory() as tmp:
            store = ProgramDefinitionStore(Path(tmp) / "programs")
            draft = store.create_draft(program, actor_id="admin")
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["program_type"], "software_delivery_ui")

    def test_reentry_works_for_implement_work_orders_ui(self):
        # Snapshot keeps resolvable built-in workflow types; the running
        # workflow_type is the clone name — reentry must not hardcode it.
        snapshot = copy.deepcopy(
            json.loads((PROGRAMS / "software_delivery.json").read_text())
        )
        snapshot["program_type"] = "software_delivery_ui"

        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-clone",
                "workflow_type": "implement_work_orders_ui",
                "program_id": "prog-ui",
                "program_phase": "implement",
                "summary": "WO-001",
            },
            "tasks": [
                {
                    "status": "succeeded",
                    "output": {
                        "work_order_id": "WO-001",
                        "pr_url": "https://example/pr/1",
                    },
                },
                {"status": "succeeded"},
            ],
        }
        client.get_program.return_value = {
            "program": {
                "current_phase": "implement",
                "program_definition_snapshot": snapshot,
                "metadata": {
                    "artifacts": {
                        "work_order_ids": ["WO-001", "WO-002"],
                        "completed_work_order_ids": ["WO-001"],
                        "planning_track": "work_orders",
                    }
                },
            }
        }
        client.advance_program.return_value = {
            "current_phase": "implement",
            "workflow_id": "wf-clone-2",
        }
        client.create_workflow.return_value = {"id": "wf-clone-2"}
        client.create_task.return_value = {"id": "t2"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-ui"}
        client.patch_program_artifacts.return_value = {}

        service = DeliveryProgramService(client=client)
        skeleton = load_skeleton("implement_work_orders")
        ctx = HookContext(
            workflow_id="wf-clone",
            workflow_type="implement_work_orders_ui",
            tenant_id="default",
            task={"id": "t-final"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras={},
        )
        with patch(
            "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
            return_value=service,
        ):
            result = complete_program_phase.handle(ctx)
        self.assertEqual(result["status"], "phase_reentered_implement")


if __name__ == "__main__":
    unittest.main()
