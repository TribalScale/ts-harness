"""The pre-push reviewer gets the code review gate's inputs: scope check, test changes, design frames."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.implementation import pre_push_review as ppr

BODY = """# PROJ-48 App shell

Design: D1 (Shell), D2 (Sidebar)

## Files
- src/shell/**
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class GateInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = repo = Path(self.tmp.name)
        _git(repo, "init", "-q", "-b", "dev")
        (repo / "src/shell").mkdir(parents=True)
        (repo / "src/shell/Shell.tsx").write_text("export const Shell = () => null\n")
        (repo / "src/shell/Shell.test.tsx").write_text("it('shows the logo', () => { expect(logo).toBeVisible() })\n")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
        _git(repo, "branch", "base")
        (repo / "src/shell/Shell.tsx").write_text("export const Shell = () => 'logo'\n")
        (repo / "src/shell/Shell.test.tsx").write_text("it('renders', () => { expect(true).toBe(true) })\n")
        (repo / "src/billing.ts").write_text("export const charge = 1\n")
        design = repo / ".ts-harness/design"
        design.mkdir(parents=True)
        (design / "D1.png").write_bytes(b"png")
        (design / "README.md").write_text("frames")
        _git(repo, "add", "src")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "change")

    def tearDown(self):
        self.tmp.cleanup()

    def inputs(self):
        refs = [{"id": "D1", "title": "Shell"}, {"id": "D2", "title": "Sidebar"}, {"id": "D3", "title": "Other"}]
        return ppr.gate_inputs(self.repo, base_ref="base", work_order_body=BODY, design_refs=refs, runner=subprocess.run)

    def test_the_local_diff_gives_the_gates_scope_and_test_change_inputs(self):
        inputs = self.inputs()
        self.assertEqual([f["file"] for f in inputs["scope_findings"]], ["src/billing.ts"])
        self.assertEqual([c["file"] for c in inputs["test_changes"]], ["src/shell/Shell.test.tsx"])

    def test_only_named_frames_that_exist_are_passed(self):
        self.assertEqual(self.inputs()["design_frames"], [{"id": "D1", "title": "Shell", "path": ".ts-harness/design/D1.png"}])

    def test_the_prompt_carries_them_and_the_gates_api_and_test_sections(self):
        prompt = ppr.build_review_prompt(
            work_order_id="PROJ-48", work_order_body=BODY, base_ref="base", output_path=Path("/tmp/out.json"),
            rubric="## Deep checks\n...", inputs=self.inputs(),
        )
        self.assertIn("Scope check", prompt)
        self.assertIn("src/billing.ts", prompt)
        self.assertIn("Tests that change meaning", prompt)
        self.assertIn(".ts-harness/design/D1.png", prompt)
        self.assertIn("API types in a web PR", ppr.GATE_SECTIONS)
        self.assertIn("Tests that change meaning", ppr.GATE_SECTIONS)

    def test_the_new_variant_is_recorded_and_can_be_switched_off(self):
        self.assertTrue(ppr.inputs_enabled())
        with mock.patch.dict("os.environ", {"TS_HARNESS_PRE_PUSH_REVIEW_INPUTS": "off"}):
            self.assertFalse(ppr.inputs_enabled())
        self.assertEqual(ppr.summary_record([], model="m", variant=ppr.VARIANT_GATE_INPUTS)["variant"], "gate_inputs")

    def test_the_gate_skill_has_the_sections_the_reviewer_reads(self):
        rubric = ppr.gate_rubric()
        self.assertIn("API types in a web PR", rubric)
        self.assertIn("Tests that change meaning", rubric)


if __name__ == "__main__":
    unittest.main()
