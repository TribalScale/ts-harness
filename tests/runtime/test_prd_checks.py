"""Automatic PRD checks (runtime/ts_harness/workflows/prd_checks.py) and where they surface."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ts_harness.agent.loop import PRD_DRAFT_MODE, PRD_INTAKE_MODE, EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.config.schema import load_runtime_config
from ts_harness.workflows.artifacts import format_artifact_for_channel
from ts_harness.skills.aliases import canonical_skill_ids
from ts_harness.tools.registry import build_default_registry
from ts_harness.workflows.prd_checks import check_prd, format_prd_checks

REPO_ROOT = Path(__file__).resolve().parents[2]

GOOD_PRD = """# PRD: Expense export

**Author:** pm-agent · **Status:** Draft · **Last updated:** 2026-11-14
**Profile:** Lite (no Full trigger)

## Overview
Finance admins export a month of approved expenses as CSV.

## Problem Statement
Finance re-keys expenses into the ledger each month.
**Current state:** a manual copy from the expenses page.
**Why now:** the Q1 audit needs a repeatable export.

## Goals
- Month-end close takes under 2 hours instead of 6.

## Non-Goals
- Scheduled delivery of the export.

## Decisions
- Only finance admins can export (from intake Q1: accepted recommendation).

## Assumptions
- One export covers a single calendar month; a custom range would add a date picker.

## Personas & Permissions
| Persona | Can | Cannot |
|---|---|---|
| Finance admin | Export | Edit expenses |

## Proposed Solution
An Export button on the expenses page writes a CSV of approved expenses for the chosen month. Use `<br>` sparingly.

## Key User Flows
### Export a month
1. Pick a month and press Export.
**On failure:** the page shows "Export failed, try again".

## Requirements
- [ ] **FR-1** (Must) Export approved expenses for one calendar month as CSV (Specified in: Proposed Solution; Verified by: DoD-1)
- [ ] **FR-2** (Must) Only finance admins see the Export button (Specified in: Personas & Permissions; Verified by: DoD-2)
- [ ] **FR-3** (Should) Show "Export failed, try again" when the export fails (Specified in: Key User Flows; Verified by: DoD-3)
- [ ] **FR-4**: ~~withdrawn, superseded by FR-1~~

## Technical Constraints
- CSV, UTF-8.

## Definition of Done
- DoD-1: A finance admin downloads a CSV with every approved expense of the month.
- DoD-2: A non-admin does not see the Export button.
- DoD-3: A failed export shows the error message.
- DoD-4: The CSV opens in Excel with correct accents.
- DoD-5: The export of 5,000 rows finishes within 10 seconds.

## Success Metrics
| Metric | Baseline | Target | Measurement window |
|---|---|---|---|
| Close time | 6 h | 2 h | first 3 month-ends |

## Open Questions
- [ ] Should rejected expenses be included later? (owner: finance lead)

```markdown
<example placeholder inside a code block is ignored> TODO
```
"""


class PrdChecksTests(unittest.TestCase):
    def test_a_complete_prd_has_no_findings(self):
        self.assertEqual(check_prd(GOOD_PRD), [])

    def test_each_problem_is_reported(self):
        bad = (
            GOOD_PRD.replace("## Decisions\n- Only finance admins can export (from intake Q1: accepted recommendation).\n", "")
            .replace("**FR-1** (Must) Export", "**FR-1** Export")
            .replace("; Verified by: DoD-2)", ")")
            .replace("Verified by: DoD-3", "Verified by: DoD-9")
            .replace("- DoD-5: The export of 5,000 rows finishes within 10 seconds.\n", "")
            .replace("(owner: finance lead)", "")
            .replace("- Scheduled delivery of the export.", "N/A: everything is in scope")
            .replace("**FR-3** (Should) Show", "**FR-3** (Should) Show a fast, intuitive message; TBD")
            .replace("Finance admins export", "<Feature summary> Finance admins export")
        )
        text = "\n".join(check_prd(bad))
        for expected in (
            "Missing sections: Decisions",
            "Template placeholders left: <Feature summary>",
            "Unresolved markers: TBD",
            '"fast" in Requirements',
            '"intuitive" in Requirements',
            "No 'Verified by' on FR-2",
            "cites DoD items that do not exist: DoD-9",
            "No priority (Must, Should or Could) on FR-1",
            "Definition of Done has 4 items",
            "Open questions without an owner",
            "Non-Goals says N/A",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        self.assertNotIn("FR-4", text)  # withdrawn
        self.assertNotIn("<br>", text)
        self.assertNotIn("TODO", text)  # inside a code block

    def test_vague_words_with_a_number_are_allowed(self):
        self.assertEqual(check_prd(GOOD_PRD.replace("finishes within 10 seconds", "is fast: within 10 seconds")), [])

    def test_format_is_empty_without_findings(self):
        self.assertEqual(format_prd_checks([]), "")
        self.assertIn("found 1 issue:\n- x", format_prd_checks(["x"]))
        self.assertEqual(check_prd("  "), ["The PRD is empty."])


class PrdChecksSurfaceTests(unittest.TestCase):
    def setUp(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        self.agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        self.pm = config.get_agent("pm-agent")

    def test_draft_output_carries_the_checks(self):
        payload = {"step_name": "draft_prd", "required_artifacts": ["prd_draft"], "skill_hints": ["prd-drafter"]}
        body = GOOD_PRD.replace("**FR-1** (Must)", "**FR-1**")
        output = self.agent._parse_output(json.dumps({"result": {"prd_markdown": body}}), payload)
        self.assertEqual(output["prd_checks"], ["No priority (Must, Should or Could) on FR-1."])
        clean = self.agent._parse_output(json.dumps({"result": {"prd_markdown": GOOD_PRD}}), payload)
        self.assertEqual(clean["prd_checks"], [])

    def test_intake_drops_questions_the_request_already_answers(self):
        payload = {"step_name": "intake", "skill_hints": ["prd-intake"]}
        asked = [
            "Should managers see the list? Recommended: yes, managers only.",
            "How should leave be handled? Recommended: skip people on leave, as stated in the constraints.",
            "Is a template needed? Recommended: yes; the brief already says email is the fallback.",
        ]
        output = self.agent._parse_output(json.dumps({"summary": "s", "questions": asked}), payload)
        self.assertEqual(output["questions"], asked[:1])
        only = self.agent._parse_output(json.dumps({"summary": "s", "questions": asked[1:2]}), payload)
        self.assertEqual(only["questions"], asked[1:2])  # the contract needs one question

    def test_intake_output_has_no_checks(self):
        payload = {"step_name": "intake", "skill_hints": ["prd-intake"]}
        output = self.agent._parse_output(json.dumps({"summary": "s", "questions": ["q?"]}), payload)
        self.assertNotIn("prd_checks", output)

    def test_findings_are_shown_to_the_reviewer_before_the_reply_prompt(self):
        draft = {"result": {"prd_markdown": GOOD_PRD}, "prd_checks": ["No priority on FR-1."]}
        message = format_artifact_for_channel(draft, display_name="PRD", present_prompt="approval")
        checks = message.index("Automatic PRD checks found 1 issue:\n- No priority on FR-1.")
        self.assertLess(checks, message.index("Reply yes to approve"))
        clean = format_artifact_for_channel({**draft, "prd_checks": []}, display_name="PRD", present_prompt="approval")
        self.assertNotIn("Automatic PRD checks", clean)

    def test_stated_times_and_limits_must_reach_requirements(self):
        stated = "Remind every Friday at 15:00, again Monday at 08:00. Exports up to 5,000 rows within 10 seconds."
        findings = check_prd(GOOD_PRD, stated=stated)
        self.assertEqual(
            findings, ["Stated times and limits not found in Requirements, Technical Constraints or Definition of Done (fine if they are only context): 15:00, 08:00."]
        )
        carried = GOOD_PRD.replace("- CSV, UTF-8.", "- CSV, UTF-8.\n- Reminders Friday 15:00 and Monday 8:00.")
        self.assertEqual(check_prd(carried, stated=stated), [])

    def test_template_header_and_an_empty_open_questions_list_are_not_findings(self):
        body = GOOD_PRD.replace(
            "**Profile:** Lite (no Full trigger)", "**Profile:** Lite (no Full trigger)\n**Linked epic:** <filled in after filing>"
        ).replace("- [ ] Should rejected expenses be included later? (owner: finance lead)", "- [ ] None")
        self.assertEqual(check_prd(body), [])

    def test_prd_steps_keep_their_guidance_on_the_contract_path(self):
        # prd_request stages have output contracts; the step guidance used to be dropped there.
        contract = {"output_schema": {"type": "object"}}
        intake = self.agent._build_workflow_system_prompt(
            self.pm, {"step_name": "intake", "skill_hints": ["prd-intake"], "contract": contract}
        )
        self.assertIn(PRD_INTAKE_MODE, intake)
        self.assertIn("Recommended:", intake)
        draft = self.agent._build_workflow_system_prompt(
            self.pm, {"step_name": "draft_prd", "skill_hints": ["prd-drafter"], "contract": contract}
        )
        self.assertIn(PRD_DRAFT_MODE, draft)
        other = self.agent._build_workflow_system_prompt(
            self.pm, {"step_name": "intake", "skill_hints": ["clarify-rfp"], "contract": contract}
        )
        self.assertNotIn("Clarifying Intake mode", other)

    def test_legacy_hints_resolve_to_the_prd_skills(self):
        self.assertEqual(canonical_skill_ids(["clarify-requirements", "draft-prd"]), ["prd-intake", "prd-drafter"])

    def test_prd_request_names_the_skills_that_exist(self):
        payload = json.loads((REPO_ROOT / "config" / "workflows" / "prd_request.json").read_text())
        hints = {stage["name"]: stage.get("skill_hints") for stage in payload["stages"]}
        self.assertEqual(hints["intake"], ["prd-intake"])
        self.assertEqual(hints["draft_prd"], ["prd-drafter"])
        for skill in ("prd-intake", "prd-drafter"):
            self.assertTrue((REPO_ROOT / "skills" / "pm" / skill / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
