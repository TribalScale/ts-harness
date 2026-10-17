from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.program_adapters import build_default_adapter_registry
from ts_harness.workflows.program_definition_store import ProgramDefinitionStore
from ts_harness.workflows.definition_store import WorkflowDefinitionStore
from ts_harness.routing.overlay import validate_overlay_payload

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


class RfpPaletteProgramTests(unittest.TestCase):
    def test_palette_shaped_rfp_workflow_validates_and_activates(self):
        payload = json.loads((FIXTURES / "rfp_review.palette.json").read_text())
        hooks = set(build_default_registry().names())
        skeleton, issues = validate_workflow_definition(
            payload,
            hook_names=hooks,
            agent_ids={"sales-agent", "orchestrator"},
            skill_ids={"clarify-rfp", "draft-rfp"},
            built_in_types={"rfp_request"},
        )
        errors = [issue for issue in issues if issue.severity == "error"]
        self.assertIsNotNone(skeleton)
        self.assertEqual(errors, [], errors)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowDefinitionStore(Path(tmp))
            draft = store.create_draft(payload, actor_id="admin")
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["workflow_type"], "rfp_review")

    def test_rfp_program_uses_declared_outputs_adapter(self):
        program = json.loads((FIXTURES / "rfp_response.program.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            store = ProgramDefinitionStore(Path(tmp))
            draft = store.create_draft(program, actor_id="admin")
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["program_type"], "rfp_response")
            active = store.get_active("rfp_response")
            assert active is not None
            node = next(n for n in active["nodes"] if n["id"] == "rfp")
            self.assertEqual(node["completion_adapter"], "extract_declared_outputs")

        registry = build_default_adapter_registry()
        patch = registry.extract_outputs(
            "extract_declared_outputs",
            workflow_type="rfp_review",
            tasks=[
                {
                    "status": "succeeded",
                    "output": {
                        "approved_artifact": {"rfp_markdown": "# Win"},
                        "rfp_markdown": "# Win",
                    },
                }
            ],
            produces_artifacts=["approved_rfp"],
        )
        self.assertEqual(patch["approved_rfp"], {"rfp_markdown": "# Win"})

    def test_routing_intent_for_rfp_program(self):
        payload = {
            "definition_type": "channel_routing",
            "definition_version": "1.0.0",
            "intents": [
                {
                    "id": "governed_rfp",
                    "signals": ["create rfp", "draft rfp", "request for proposal"],
                    "program_type": "rfp_response",
                    "entry_phase": "rfp",
                    "policy": {
                        "mode": "require_workflow",
                        "deny_tools": ["propose_delegate_task", "delegate_task"],
                    },
                }
            ],
        }
        canonical, issues = validate_overlay_payload(
            payload,
            known_program_types={"rfp_response"},
            known_workflow_types={"rfp_review"},
        )
        self.assertIsNotNone(canonical)
        self.assertFalse(any(issue["severity"] == "error" for issue in issues))


if __name__ == "__main__":
    unittest.main()
