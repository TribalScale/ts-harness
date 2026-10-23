"""Tests for PRD intake wait/resume flow."""

from __future__ import annotations

import unittest

from ts_harness.workflows.expressions import resolve_input_map, resolve_token
from ts_harness.workflows.intake import (
    AWAIT_INTAKE_STEP,
    INTAKE_RESPONSE_EVENT,
    intake_original_request,
    intake_task_output,
    merge_intake_response,
    task_awaiting_intake_response,
)


class IntakeWaitTests(unittest.TestCase):
    def test_task_awaiting_intake_response_detects_blocked_step(self):
        tasks = [
            {"step_name": "intake", "status": "succeeded"},
            {
                "id": "wait-1",
                "step_name": AWAIT_INTAKE_STEP,
                "status": "blocked",
                "error": f"awaiting_external:{INTAKE_RESPONSE_EVENT}",
            },
        ]
        waiting = task_awaiting_intake_response(tasks)
        self.assertIsNotNone(waiting)
        assert waiting is not None
        self.assertEqual(waiting["id"], "wait-1")

    def test_merge_intake_response_builds_clarified_requirements(self):
        merged = merge_intake_response(
            {"summary": "PRD for checker game", "questions": ["Platform?"]},
            "Web and mobile",
            summary="PRD for checker game",
        )
        self.assertEqual(merged["user_response"], "Web and mobile")
        clarified = merged["clarified_requirements"]
        self.assertEqual(clarified["answers"], "Web and mobile")
        self.assertEqual(clarified["questions"], ["Platform?"])
        self.assertEqual(clarified["initial_request"], "PRD for checker game")

    def test_initial_request_is_the_requesters_full_text(self):
        request = "Create a PRD for a React dashboard.\n1. Track BTC, ETH, SOL every 3 seconds."
        tasks = [
            {
                "step_name": "intake",
                "status": "succeeded",
                "created_at": "2026-10-22T02:00:00Z",
                "input": {
                    "summary": request[:20],
                    "context": {
                        "summary": request[:20],
                        "transcript": [
                            {"role": "user", "content": request},
                            {"role": "assistant", "content": "I've prepared a prd request workflow."},
                        ],
                    },
                },
                "output": {"summary": "Questions: which chart?", "questions": ["Chart?"]},
            },
        ]
        original = intake_original_request(tasks)
        self.assertEqual(original, request)
        merged = merge_intake_response(
            intake_task_output(tasks),
            "A donut chart",
            original_request=original,
        )
        clarified = merged["clarified_requirements"]
        self.assertEqual(clarified["initial_request"], request)
        self.assertEqual(clarified["summary"], "Questions: which chart?")

    def test_initial_request_falls_back_to_the_intake_summary(self):
        tasks = [
            {
                "step_name": "intake",
                "status": "succeeded",
                "input": {"summary": "Checker game PRD"},
                "output": {},
            },
        ]
        self.assertEqual(intake_original_request(tasks), "Checker game PRD")
        self.assertEqual(intake_original_request([]), "")

    def test_resolve_nested_prior_output_token(self):
        tasks = [
            {
                "step_name": "await_intake_answers",
                "status": "succeeded",
                "output": {
                    "clarified_requirements": {"answers": "Web and mobile"},
                },
            }
        ]
        value = resolve_token(
            "$prior.await_intake_answers.output.clarified_requirements",
            tasks=tasks,
        )
        self.assertEqual(value, {"answers": "Web and mobile"})

    def test_resolve_input_map_for_draft_prd(self):
        tasks = [
            {
                "step_name": "intake",
                "status": "succeeded",
                "output": {"summary": "Checker game"},
            },
            {
                "step_name": "await_intake_answers",
                "status": "succeeded",
                "output": {
                    "clarified_requirements": {"answers": "Web and mobile"},
                },
            },
        ]
        resolved = resolve_input_map(
            {
                "clarified_requirements": "$prior.await_intake_answers.output.clarified_requirements",
                "intake_output": "$prior.intake.output",
            },
            tasks=tasks,
        )
        self.assertEqual(resolved["clarified_requirements"], {"answers": "Web and mobile"})
        self.assertEqual(resolved["intake_output"], {"summary": "Checker game"})


if __name__ == "__main__":
    unittest.main()
