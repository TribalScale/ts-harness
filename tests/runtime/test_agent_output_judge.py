"""Judge scorer. Does not call a live model."""

from __future__ import annotations

import unittest

from ts_harness.evals.output_judge import load_dataset, score_judgment


class OutputJudgeScoreTests(unittest.TestCase):
    def test_dataset_has_a_rubric_per_case(self):
        cases = load_dataset()
        self.assertGreaterEqual(len(cases), 2)
        for case in cases:
            self.assertTrue(case["id"])
            self.assertTrue(case["agent_id"])
            self.assertTrue(case["user_text"])
            self.assertTrue(case["rubric"])

    def test_score_judgment_accepts_only_an_explicit_pass(self):
        self.assertEqual(score_judgment('{"pass": true, "reason": "answers the question"}'), [])
        self.assertEqual(
            score_judgment('{"pass": false, "reason": "claims it edited the repo"}'),
            ["claims it edited the repo"],
        )
        self.assertEqual(score_judgment("not json"), ["judge response was not json"])
        self.assertEqual(score_judgment('```json\n{"pass": true}\n```'), [])
