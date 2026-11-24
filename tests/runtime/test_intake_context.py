"""The intake steps see the request in the person's own words, and the files it names."""

from __future__ import annotations

import unittest

from ts_harness.workflows.bug_context import cited_paths
from ts_harness.workflows.program_adapters import _build_declared_inputs, requester_context

TREE = [
    "src/features/schedule/WeekView.tsx",
    "src/features/schedule/DayView.tsx",
    "src/api/health.d.ts",
    "src/api/hub.d.ts",
    "src/api/schedule.d.ts",
    "src/api/schema.d.ts",
    "src/other/WeekView.test.tsx",
    "lib/a/Shared.ts",
    "lib/b/Shared.ts",
]


class RequesterContextTests(unittest.TestCase):
    def test_the_persons_messages_reach_the_intake(self):
        history = [
            {"role": "user", "content": "File a bug: src/features/schedule/WeekView.tsx:221 crashes"},
            {"role": "assistant", "content": "Proposed. Reply yes."},
            {"role": "user", "content": "yes"},
        ]
        intake = _build_declared_inputs(summary="short paraphrase", artifacts={}, transcript_messages=history)
        self.assertIn("src/features/schedule/WeekView.tsx:221", intake["context"])
        self.assertNotIn("Proposed", intake["context"])

    def test_no_history_adds_nothing(self):
        self.assertEqual(requester_context(None), "")
        self.assertNotIn("context", _build_declared_inputs(summary="s", artifacts={}))


class CitedPathTests(unittest.TestCase):
    def test_a_bare_file_name_names_the_one_file_with_that_name(self):
        self.assertEqual(cited_paths("WeekView.tsx:221 reads carryOver", TREE), ["src/features/schedule/WeekView.tsx"])

    def test_an_ambiguous_bare_name_names_nothing(self):
        self.assertEqual(cited_paths("Shared.ts breaks", TREE), [])

    def test_a_named_folder_with_its_files_listed_by_stem(self):
        report = "Files in src/api/ are hand-written: health, hub (.d.ts). The crash is in WeekView.tsx."
        self.assertEqual(
            cited_paths(report, TREE),
            ["src/features/schedule/WeekView.tsx", "src/api/health.d.ts", "src/api/hub.d.ts"],
        )


if __name__ == "__main__":
    unittest.main()
