"""Screen specs drafted from Figma frames, corrected at intake."""

from __future__ import annotations

import json
import unittest

from ts_harness.intake import screen_specs as ss
from ts_harness.workflows.artifacts import format_artifact_for_channel


def _ref(i: int, title: str, **extra) -> dict:
    return {"id": f"D{i}", "title": title, "source": "figma", "url": f"https://www.figma.com/design/F/?node-id=1-{i}",
            "mime": "image/png", "object_key": f"design/t/{i}.png", "summary": f"Frame: {title}\nText: Save | Cancel", **extra}


REFS = [
    _ref(1, "Hub / Schedule / 02 Week · Base"),
    _ref(2, "Hub / Schedule / 02 Week · Pilot"),
    _ref(3, "Hub / Holds / 01 List"),
    {"id": "D4", "title": "screenshot.png", "source": "upload", "object_key": "design/t/4.png"},
]

REPLY = {
    "purpose": "Plan the week by machine.",
    "users": "Finite team",
    "layout": ["Top bar", "Machine rows by day"],
    "components": ["Week picker", "Order blocks"],
    "data_shown": [{"field": "Order number", "example": "1034036"}],
    "actions": [{"control": "Drag a block", "appears_to": "move the order"}],
    "states": ["Pilot banner (D2)"],
    "questions": [
        {"question": "Should the card icons have tooltips?", "recommended": "Yes", "impact": "low"},
        {"question": "What happens when a block is dropped on a machine that is down?", "recommended": "Block the drop and say why", "impact": "high"},
    ],
}


class Store:
    def get(self, key):
        return b"png-" + key.encode()


class ScreenSpecTests(unittest.TestCase):
    def test_frames_group_into_screens_by_name(self):
        self.assertEqual(ss.screen_name("Hub / Schedule / 02 Week · Base"), "Hub / Schedule / 02 Week")
        self.assertEqual(ss.screen_name("Admin / Users — Empty"), "Admin / Users")
        screens = ss.group_screens(REFS)
        self.assertEqual([(s["screen"], [f["id"] for f in s["frames"]]) for s in screens],
                         [("Hub / Schedule / 02 Week", ["D1", "D2"]), ("Hub / Holds / 01 List", ["D3"])])

    def test_specs_questions_and_documents(self):
        seen = []

        def describe(prompt, images):
            seen.append((prompt, images))
            return "```json\n" + json.dumps(REPLY) + "\n```"

        out = ss.draft_screen_specs(REFS, describe=describe, store=Store())
        self.assertFalse(out["skipped"])
        self.assertEqual(len(seen), 2)
        week_prompt, week_images = next(item for item in seen if "02 Week" in item[0])
        self.assertEqual(len(week_images), 2)
        self.assertIn('D1 "Hub / Schedule / 02 Week · Base"', week_prompt)
        self.assertIn("Text: Save | Cancel", week_prompt)
        first = out["screen_specs"][0]
        self.assertEqual((first["id"], first["purpose"], first["questions"][0]["id"]), ("SC1", "Plan the week by machine.", "S1"))
        # Highest impact first across screens (S2 and S4 are high), screen order within an impact.
        self.assertEqual([q.split(".")[0] for q in out["screen_questions"]], ["S2", "S4", "S1", "S3"])
        self.assertEqual(out["screen_questions"][0], "S2. Hub / Schedule / 02 Week: What happens when a block is dropped on a machine that is down? Recommended: Block the drop and say why.")
        doc = out["summary"]
        self.assertIn("## SC1 · Hub / Schedule / 02 Week", doc)
        self.assertIn("[D1 Hub / Schedule / 02 Week · Base](https://www.figma.com/design/F/?node-id=1-1)", doc)
        self.assertIn("| Order number | 1034036 |", doc)
        self.assertIn("**S2** (high) What happens when a block is dropped", doc)
        self.assertIn("**S1** (low) Should the card icons have tooltips?", doc)
        self.assertIn("### SC2 Hub / Holds / 01 List (frames D3)", out["screen_specs_text"])
        self.assertIn("Question S3:", out["screen_specs_text"])

    def test_one_screen_failing_leaves_the_others(self):
        def describe(prompt, images):
            if "Holds" in prompt:
                return "not json"
            return json.dumps(REPLY)

        out = ss.draft_screen_specs(REFS, describe=describe, store=Store())
        self.assertIn("error", out["screen_specs"][1])
        self.assertEqual(len(out["screen_questions"]), 2)
        self.assertIn("Not drafted: Hub / Holds / 01 List.", out["summary"])

    def test_no_figma_frames_skips_quietly(self):
        out = ss.draft_screen_specs([REFS[3]], describe=lambda p, i: "{}", store=Store())
        self.assertEqual((out["skipped"], out["screen_questions"]), (True, []))
        self.assertTrue(ss.draft_screen_specs(REFS, describe=None, store=Store())["skipped"])


class IntakeMessageTests(unittest.TestCase):
    def test_screen_questions_get_their_own_section(self):
        output = {
            "summary": "I read the sources; two decisions are left.",
            "questions": ["Should admins edit machines? Recommended: yes."],
            "screen_questions": ["S2. Hub / Schedule / 02 Week: How does it work on a phone? Recommended: a day list."],
        }
        text = format_artifact_for_channel(output, display_name="PRD review", present_prompt="intake",
                                           presentation={"questions": "all"})
        self.assertIn("1. Should admins edit machines?", text)
        self.assertIn("**Screen questions** (answer by S-number):\nS2. Hub / Schedule / 02 Week", text)
        self.assertIn("Every other question in the screen specs keeps its recommended answer", text)
        many = {**output, "screen_questions": [f"S{i}. Screen: Q{i}? Recommended: yes." for i in range(1, 31)]}
        listed = format_artifact_for_channel(many, display_name="PRD review", present_prompt="intake", presentation={"questions": "all"}, max_length=100000)
        self.assertIn("S20. Screen: Q20?", listed)
        self.assertNotIn("S21. Screen", listed)
        self.assertTrue(text.rstrip().endswith("then say ready when you want a draft."))


if __name__ == "__main__":
    unittest.main()


class SystemStepTests(unittest.TestCase):
    def test_the_step_reads_the_gathered_frames(self):
        from unittest import mock

        from ts_harness.workflows.system_steps import run_system_step

        tasks = [{"step_name": "gather_sources", "status": "succeeded", "output": {"design_refs": REFS}}]
        with mock.patch("ts_harness.workflows.hooks._helpers.load_workflow_payload", return_value=({"tenant_id": "t"}, tasks)), \
                mock.patch("ts_harness.intake.screen_specs.build_describer", return_value=lambda p, i: json.dumps(REPLY)), \
                mock.patch("ts_harness.intake.blobs.intake_store", return_value=Store()):
            result = run_system_step("draft_screen_specs", {"workflow_id": "w1"}, {"agent_id": "pm-agent"}, memory=mock.Mock())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["output"]["screen_specs"]), 2)
        with mock.patch("ts_harness.workflows.hooks._helpers.load_workflow_payload", return_value=({}, [])):
            skipped = run_system_step("draft_screen_specs", {"workflow_id": "w1"}, {}, memory=mock.Mock())
        self.assertIs(skipped["output"]["skipped"], True)
