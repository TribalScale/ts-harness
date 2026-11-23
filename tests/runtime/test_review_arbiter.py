"""Arbitration between code review and QA: the model rules, the policy decides."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from ts_harness.workflows import review_arbiter as ra

RUN = [
    {"step": "finalize_code_review", "feedback": "sort the rows explicitly"},
    {"step": "qa_gate", "feedback": "explicit sorting breaks 'keep the existing order'"},
    {"step": "finalize_code_review", "feedback": "the capacity rounds 209 h up to 210 h"},
]


def _ruling(**overrides):
    base = {"contradiction": True, "disputes": [{"code_review": "sort", "qa": "keep order",
            "ruling": "QA stands: the work order says keep the existing order"}],
            "needs_product_decision": False, "security": False,
            "guidance": "Keep the existing order; compute capacity from hours, not rounded days; add a 209 h test.",
            "reason": "the work order settles it"}
    return {**base, **overrides}


class DecideTests(unittest.TestCase):
    def test_a_settled_dispute_continues(self):
        decision = ra.decide(_ruling(), autonomy="autonomous", total=3, run=RUN)
        self.assertEqual(decision["decision"], "continue")

    def test_the_policy_asks_a_person(self):
        cases = [
            (dict(ruling=_ruling(), autonomy="conservative", total=1), "asks a person"),
            (dict(ruling=_ruling(), autonomy="autonomous", total=8), "8 revisions"),
            (dict(ruling=None, autonomy="autonomous", total=1), "could not be arbitrated"),
            (dict(ruling=_ruling(security=True), autonomy="autonomous", total=1), "security"),
            (dict(ruling=_ruling(needs_product_decision=True, reason="the order of rows is a product call"),
                  autonomy="autonomous", total=1), "product call"),
            (dict(ruling=_ruling(guidance=""), autonomy="autonomous", total=1), "no instruction"),
        ]
        for kwargs, why in cases:
            decision = ra.decide(kwargs["ruling"], autonomy=kwargs["autonomy"], total=kwargs["total"], run=RUN)
            self.assertEqual(decision["decision"], "escalate", why)
            self.assertIn(why, decision["reason"])

    def test_security_words_in_the_feedback_ask_even_if_the_model_missed_them(self):
        run = [*RUN, {"step": "qa_gate", "feedback": "the session token is logged"}]
        self.assertEqual(ra.decide(_ruling(), autonomy="autonomous", total=1, run=run)["decision"], "escalate")


class PlanTests(unittest.TestCase):
    def _plan(self, reply, **kwargs):
        calls = []

        def model_call(prompt):
            calls.append(prompt)
            return reply

        tasks = [{"step_name": "deliver_work_order", "status": "succeeded", "input": {"work_order_body": "Keep the existing order."}}]
        return ra.plan(RUN, tasks=tasks, workflow={}, memory=MagicMock(), implement_step="deliver_work_order",
                       model_call=model_call, autonomy="autonomous", **kwargs), calls

    def test_a_ruling_becomes_the_revision_feedback(self):
        decision, calls = self._plan("Here: " + json.dumps(_ruling()))
        self.assertEqual(decision["decision"], "continue")
        self.assertIn("QA stands", decision["feedback"])
        self.assertIn("compute capacity from hours", decision["feedback"])
        self.assertIn("1. Code review:", calls[0])
        self.assertIn("2. QA:", calls[0])

    def test_independent_findings_are_all_fixed(self):
        decision, _ = self._plan(json.dumps(_ruling(contradiction=False, disputes=[])))
        self.assertIn("different problems, not opposite ones", decision["feedback"])

    def test_no_usable_reply_asks_a_person(self):
        decision, _ = self._plan("I cannot decide")
        self.assertEqual(decision["decision"], "escalate")


if __name__ == "__main__":
    unittest.main()


class SecurityWordsTests(unittest.TestCase):
    def test_the_verb_authorize_is_not_a_security_finding(self):
        from ts_harness.workflows.revision_triage import security_sensitive

        prose = {"message": "REQ-PKG-005 does not authorize replacing that order with a load-balancing penalty"}
        self.assertFalse(security_sensitive(prose))
        for text in ("missing authorization check on /admin", "unauthorized users can read orders",
                     "the session token is logged", "auth middleware skipped", "password stored in plain text"):
            self.assertTrue(security_sensitive({"message": text}), text)
