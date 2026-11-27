"""The pre-push review trial: it reviews with the code review gate's own rubric."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.implementation import pre_push_review as ppr


class RubricTests(unittest.TestCase):
    def test_the_gates_judging_sections_are_read_from_its_skill(self):
        rubric = ppr.gate_rubric()
        headings = [line for line in rubric.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, [f"## {title}" for title in ppr.GATE_SECTIONS])
        self.assertIn("A `blocker` in any category", rubric)
        self.assertNotIn("## Output Contract", rubric)
        self.assertNotIn("## Start Here", rubric)

    def test_sections_keep_the_skill_order_and_drop_the_rest(self):
        text = "# T\n\n## Keep\na\n## Drop\nb\n## Also\nc\n"
        self.assertEqual(ppr.extract_sections(text, ("Also", "Keep")), "## Keep\na\n## Also\nc")


class PromptTests(unittest.TestCase):
    def test_the_gate_rubric_replaces_the_hand_written_checks(self):
        prompt = ppr.build_review_prompt(
            work_order_id="WO-1", work_order_body="Do X", base_ref="origin/dev",
            output_path=Path("/tmp/out.json"), rubric="## Severity\nA `blocker` blocks.",
        )
        self.assertIn("exactly as the gate below will review the pull request", prompt)
        self.assertIn("## Severity\nA `blocker` blocks.", prompt)
        self.assertIn("git diff origin/dev...HEAD", prompt)
        self.assertNotIn("deep checks wherever the change touches them", prompt)

    def test_without_a_rubric_the_legacy_prompt_runs(self):
        prompt = ppr.build_review_prompt(work_order_id="WO-1", work_order_body="Do X", base_ref="origin/dev",
                                         output_path=Path("/tmp/out.json"))
        self.assertIn("deep checks wherever the change touches them", prompt)


class SettingsTests(unittest.TestCase):
    def test_the_trial_is_the_default_and_legacy_can_be_chosen(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(ppr.review_variant(), ppr.VARIANT_GATE)
        with patch.dict("os.environ", {"TS_HARNESS_PRE_PUSH_REVIEW_VARIANT": "legacy"}):
            self.assertEqual(ppr.review_variant(), ppr.VARIANT_LEGACY)

    def test_an_implement_job_carries_the_rubric_and_records_the_variant(self):
        from ts_harness.implementation import coding_job

        with patch.object(coding_job, "_guided_model", return_value="claude-opus-5-5"), \
                patch.dict("os.environ", {"TS_HARNESS_PRE_PUSH_REVIEW_VARIANT": "gate_rubric"}):
            settings = coding_job._review_settings("implement", "anthropic")
        # The rubric now comes with the gate's inputs (scope check, test changes, design frames).
        self.assertEqual(settings["review_variant"], "gate_inputs")
        self.assertIn("## Severity", settings["review_rubric"])
        with patch.object(coding_job, "_guided_model", return_value="claude-opus-5-5"), \
                patch.dict("os.environ", {"TS_HARNESS_PRE_PUSH_REVIEW_VARIANT": "gate_rubric",
                                          "TS_HARNESS_PRE_PUSH_REVIEW_INPUTS": "off"}):
            self.assertEqual(coding_job._review_settings("implement", "anthropic")["review_variant"], "gate_rubric")

    def test_the_result_says_which_variant_ran(self):
        record = ppr.summary_record([{"status": "pass", "summary": "ok", "findings": []}], model="m", variant="gate_rubric")
        self.assertEqual(record["variant"], "gate_rubric")
        self.assertEqual(ppr.summary_record([], model="m")["variant"], "legacy")


if __name__ == "__main__":
    unittest.main()
