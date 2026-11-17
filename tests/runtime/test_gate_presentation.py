from __future__ import annotations

import copy
import unittest

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.artifacts import format_artifact_for_channel, sanitize_markdown_artifact
from ts_harness.workflows.definition_validation import validate_workflow_definition
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.presentation import (
    infer_present_body,
    resolve_present_prompt,
    schema_string_path_status,
    stage_presentation,
)

ensure_scripts_importable()

import workflow_definitions  # noqa: E402

_BRIEF_SCHEMA = {
    "type": "object",
    "required": ["result"],
    "properties": {
        "result": {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "brief_text": {"type": "string"},
                "score": {"type": "number"},
            },
        },
        "notes": {"type": "string"},
    },
}


def _custom_workflow() -> dict:
    return {
        "workflow_type": "market_brief",
        "definition_version": "1.0.0",
        "contracts": {
            "brief": {
                "presentation": {"body": "result.brief_text", "title": "result.headline"},
                "output_schema": copy.deepcopy(_BRIEF_SCHEMA),
            },
            "memo": {
                "output_schema": {
                    "type": "object",
                    "properties": {"memo_markdown": {"type": "string"}},
                }
            },
        },
        "stages": [
            {
                "name": "write_brief",
                "kind": "task",
                "agent_id": "agent-a",
                "contract_ref": "brief",
            },
            {
                "name": "write_memo",
                "kind": "task",
                "agent_id": "agent-a",
                "contract_ref": "memo",
                "depends_on": ["write_brief"],
            },
            {
                "name": "sign_off",
                "kind": "task",
                "agent_id": "human",
                "approval_required": True,
                "depends_on": ["write_memo"],
                "on_reject": {
                    "hook": "revise_on_reject",
                    "revise": {"step_name": "write_memo", "present_prompt": "approval"},
                },
            },
        ],
    }


class PresentationConfigTests(unittest.TestCase):
    def test_contract_presentation_and_stage_override(self):
        skeleton = workflow_definitions.load_skeleton_payload(_custom_workflow())
        self.assertEqual(
            stage_presentation(skeleton, "write_brief"),
            {"body": "result.brief_text", "title": "result.headline"},
        )

        payload = _custom_workflow()
        payload["stages"][0]["present_body"] = "notes"
        skeleton = workflow_definitions.load_skeleton_payload(payload)
        self.assertEqual(stage_presentation(skeleton, "write_brief")["body"], "notes")

    def test_body_is_inferred_from_markdown_field_without_config(self):
        skeleton = workflow_definitions.load_skeleton_payload(_custom_workflow())
        self.assertEqual(stage_presentation(skeleton, "write_memo"), {"body": "memo_markdown"})
        self.assertEqual(
            infer_present_body(
                {"anyOf": [{"properties": {"result": {"type": "object", "properties": {"x_markdown": {"type": "string"}}}}}]}
            ),
            "result.x_markdown",
        )

    def test_invalid_presentation_config_is_rejected(self):
        payload = _custom_workflow()
        payload["contracts"]["brief"]["presentation"]["colour"] = "red"
        with self.assertRaises(ValueError):
            workflow_definitions.load_skeleton_payload(payload)

        payload = _custom_workflow()
        payload["stages"][0]["present_body"] = "result..brief"
        with self.assertRaises(ValueError):
            workflow_definitions.load_skeleton_payload(payload)

    def test_revise_prompt_comes_from_any_gate_that_revises_the_step(self):
        skeleton = workflow_definitions.load_skeleton_payload(_custom_workflow())
        self.assertEqual(
            resolve_present_prompt(skeleton.stage("write_memo"), skeleton, []),
            "approval",
        )


class PresentationRenderingTests(unittest.TestCase):
    def test_configured_body_and_title_render_for_new_field_names(self):
        skeleton = workflow_definitions.load_skeleton_payload(_custom_workflow())
        artifact = {
            "result": {
                "headline": "EU payments brief",
                "brief_text": "Sure! Here is the brief.\n# EU payments\n\nInstant payments are mandatory.",
                "score": 4,
            },
            "notes": "internal",
        }
        message = format_artifact_for_channel(
            artifact,
            display_name="Brief",
            present_prompt="approval",
            step_name="write_brief",
            presentation=stage_presentation(skeleton, "write_brief"),
        )
        self.assertIn("EU payments brief", message)
        self.assertIn("Instant payments are mandatory.", message)
        self.assertNotIn("Sure! Here is", message)
        self.assertNotIn("internal", message)

    def test_missing_configured_field_falls_back_to_default_body(self):
        message = format_artifact_for_channel(
            {"memo_markdown": "# Memo\n\nShip on Friday."},
            display_name="Memo",
            presentation={"body": "result.not_there"},
        )
        self.assertIn("Ship on Friday.", message)

    def test_structural_cleanup_of_fences_and_closing_remarks(self):
        wrapped = "Here you go:\n```markdown\n# Plan\n\nStep one.\n```\nLet me know if you want changes."
        self.assertEqual(sanitize_markdown_artifact(wrapped), "# Plan\n\nStep one.")

        stray = "# Plan\n\nStep one.\n```\nHope this helps! Reply with edits."
        self.assertEqual(sanitize_markdown_artifact(stray), "# Plan\n\nStep one.")

        prose_first = "The crash comes from a null session.\n\nIt started after the 2.3 upgrade.\n\n## Fix\n\nGuard it."
        self.assertEqual(sanitize_markdown_artifact(prose_first), prose_first)


class PresentationValidationTests(unittest.TestCase):
    def _codes(self, payload):
        _skeleton, issues = validate_workflow_definition(
            payload,
            hook_names=set(build_default_registry().names()),
            agent_ids={"agent-a", "human"},
            skill_ids=set(),
            built_in_types=set(),
        )
        return [(issue.code, issue.path) for issue in issues if issue.code == "presentation_path_unresolved"]

    def test_paths_must_name_string_fields(self):
        self.assertEqual(self._codes(_custom_workflow()), [])

        payload = _custom_workflow()
        payload["contracts"]["brief"]["presentation"]["body"] = "result.score"
        payload["stages"][1]["present_body"] = "memo_text"
        self.assertEqual(
            self._codes(payload),
            [
                ("presentation_path_unresolved", "$.contracts.brief.presentation.body"),
                ("presentation_path_unresolved", "$.stages[1].present_body"),
            ],
        )

    def test_schema_path_status(self):
        self.assertEqual(schema_string_path_status(_BRIEF_SCHEMA, "result.brief_text"), "string")
        self.assertEqual(schema_string_path_status(_BRIEF_SCHEMA, "result.score"), "other")
        self.assertEqual(schema_string_path_status(_BRIEF_SCHEMA, "result.nope"), "missing")
        self.assertEqual(schema_string_path_status({"type": "object"}, "anything"), "string")


if __name__ == "__main__":
    unittest.main()
