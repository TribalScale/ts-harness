"""Required routing and phrase checks. No live model."""

from __future__ import annotations

import unittest

from ts_harness.evals.quality import (
    phrase_failures,
    routing_failures,
    score_routing,
    score_summary,
)
from ts_harness.routing.intents import ChannelIntent


class RoutingScoreTests(unittest.TestCase):
    def test_score_routing_reports_a_wrong_kind(self):
        failures = score_routing(ChannelIntent(kind="ad_hoc_delegate"), {"kind": "governed_prd"})
        self.assertEqual(failures, ["kind ad_hoc_delegate"])

    def test_classifier_matches_the_expected_routes(self):
        self.assertEqual(routing_failures(), [])


class PhraseScoreTests(unittest.TestCase):
    def test_score_summary_requires_phrases_and_rejects_forbidden_ones(self):
        case = {"required_phrases": ["QA passed"], "forbidden_phrases": ["rm -rf"]}
        self.assertEqual(score_summary("QA passed. No blockers.", case), [])
        self.assertIn("missing QA passed", score_summary("all good", case))
        self.assertIn("forbidden rm -rf", score_summary("QA passed then rm -rf", case))

    def test_scripted_summary_survives_to_the_user_visible_output(self):
        self.assertEqual(phrase_failures(), [])
