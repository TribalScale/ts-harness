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


class OutputJudgeRetryTests(unittest.TestCase):
    """A case must fail twice to fail the gate; the detail records failures and retries."""

    def run_with(self, results):
        from unittest.mock import MagicMock

        from ts_harness.evals.output_judge import run_live_report

        calls = {case["id"]: list(results.get(case["id"], [[]])) for case in load_dataset()}

        def attempt(config, case):
            queue = calls[case["id"]]
            return queue.pop(0) if queue else []

        return run_live_report(MagicMock(), attempt=attempt), calls

    def test_all_pass(self):
        (ok, detail), _ = self.run_with({})
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_one_flaky_case_passes_and_is_recorded(self):
        (ok, detail), _ = self.run_with({"qa-readonly": [["claimed it ran tests"], []]})
        self.assertTrue(ok)
        self.assertIn("qa-readonly passed on retry after: claimed it ran tests", detail)

    def test_failing_twice_fails_with_both_reasons(self):
        (ok, detail), _ = self.run_with({"review-readonly": [["said merged"], ["said deployed"]]})
        self.assertFalse(ok)
        self.assertIn("review-readonly: said merged / retry: said deployed", detail)

    def test_only_one_retry(self):
        (ok, _), calls = self.run_with({"qa-readonly": [["a"], ["b"], []]})
        self.assertFalse(ok)
        self.assertEqual(calls["qa-readonly"], [[]])  # the third answer was never asked for
