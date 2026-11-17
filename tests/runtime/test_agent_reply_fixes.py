from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ts_harness.agent.loop import EnterpriseAgent, _finalize_prd_output
from ts_harness.agent.provider import FakeProvider
from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway.channel_confirmations import (
    PROPOSAL_FORMATTERS,
    format_program_proposal_message,
    with_confirmation_prompt,
)
from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.routing.intents import REQUEST_TEXT_LIMIT, classify_channel_intent
from ts_harness.tools.registry import build_default_registry
from ts_harness.workflows.expressions import resolve_value

ensure_scripts_importable()

import workflow_definitions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _agent(responses):
    config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
    provider = FakeProvider(responses)
    return EnterpriseAgent(config, build_default_registry(), provider=provider), provider


def _tool_call_turn(index: int) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {"name": "no_such_tool", "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }


class IntakeQuestionParserTests(unittest.TestCase):
    def setUp(self):
        self.agent, _provider = _agent([])

    def test_hint_after_question_mark_does_not_swallow_the_next_question(self):
        text = (
            "Before I draft, a few questions:\n"
            "1. Who sees the Revisions card? For example admins only, or every editor.\n"
            "2. How long until a draft counts as stale?\n"
            "3. Should Discard ask for confirmation?\n"
            "Thanks!"
        )
        self.assertEqual(
            self.agent._extract_intake_questions(text),
            [
                "Who sees the Revisions card? For example admins only, or every editor.",
                "How long until a draft counts as stale?",
                "Should Discard ask for confirmation?",
            ],
        )

    def test_wrapped_list_item_is_one_question(self):
        text = "- Which roles may delete a draft\n  and should that be audited?\n- What is the retention period?"
        self.assertEqual(
            self.agent._extract_intake_questions(text),
            [
                "Which roles may delete a draft and should that be audited?",
                "What is the retention period?",
            ],
        )

    def test_prose_question_keeps_text_up_to_the_last_question_mark(self):
        self.assertEqual(
            self.agent._extract_intake_questions("Is this for web only? Let me know."),
            ["Is this for web only?"],
        )


class ConfirmationPromptTests(unittest.TestCase):
    def test_prompt_is_appended_only_when_missing(self):
        self.assertIn("Reply **yes**", with_confirmation_prompt("I'll link this thread to the program."))
        already = "Ready. Reply yes to start."
        self.assertEqual(with_confirmation_prompt(already), already)

    def test_program_proposal_uses_the_deterministic_message(self):
        self.assertIs(PROPOSAL_FORMATTERS["propose_start_program"], format_program_proposal_message)
        message = PROPOSAL_FORMATTERS["propose_start_program"](
            {"program_type": "feature_delivery", "entry_phase": "prd", "summary": "Revisions card"}
        )
        self.assertIn("Reply **yes** to start", message)

    def test_long_request_is_shortened_only_in_the_message(self):
        request = "The settings page shows the wrong revision. " * 20 + "See runtime/app/settings.py."
        message = format_program_proposal_message({"program_type": "bug", "summary": request})
        self.assertLess(len(message), 500)
        self.assertNotIn("settings.py", message)
        self.assertGreater(REQUEST_TEXT_LIMIT, len(request))

    def test_classified_intent_keeps_the_whole_request(self):
        request = "File this bug. " + "The settings page previews a stale draft. " * 12 + "See runtime/app/settings_bff.py."
        self.assertGreater(len(request), 400)
        self.assertEqual(classify_channel_intent(request, {}).summary, request)
        self.assertEqual(len(classify_channel_intent("x" * 9000, {}).summary), REQUEST_TEXT_LIMIT)


class PrdFinalizeTests(unittest.TestCase):
    def test_last_updated_is_stamped_and_summary_derived_from_the_prd(self):
        output = {
            "summary": "I had trouble recalling prior memories, but here is the PRD.",
            "result": {
                "prd_title": "Revisions card",
                "prd_profile": "lite",
                "prd_markdown": "# PRD: Revisions card\n\n**Author:** pm-agent · **Status:** Draft · **Last updated:** 2023-10-05\n\n## Overview\nx",
            },
        }
        final = _finalize_prd_output(output, {"step_name": "draft_prd", "input": {"today": "2026-10-30"}})
        self.assertIn("**Last updated:** 2026-10-30\n", final["result"]["prd_markdown"])
        self.assertIn("**Status:** Draft ·", final["result"]["prd_markdown"])
        self.assertEqual(final["summary"], "Drafted PRD: Revisions card (lite profile)")

    def test_missing_today_falls_back_to_utc_date(self):
        final = _finalize_prd_output(
            {"prd_markdown": "# PRD: X\n\n**Last updated:** <YYYY-MM-DD> · **Profile:** Lite"},
            {"step_name": "revise_prd", "input": {}},
        )
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertIn(f"**Last updated:** {today} ·", final["prd_markdown"])
        self.assertEqual(final["summary"], "Revised PRD: X")

    def test_runtime_today_expression(self):
        value = resolve_value("$runtime.today", tasks=[], workflow={}, reject_comments="", source_steps=[])
        self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


class StageTurnLimitTests(unittest.TestCase):
    def test_payload_max_turns_overrides_the_agent_default(self):
        agent, provider = _agent([_tool_call_turn(index) for index in range(20)])
        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={"task_id": "1", "step_name": "file_things", "input": {}, "max_turns": 12},
        )
        self.assertEqual(result.error, "max iterations exceeded")
        self.assertEqual(len(provider.calls), 12)

    def test_stage_retry_max_turns_loads_and_is_bounded(self):
        def payload(max_turns):
            return {
                "workflow_type": "turns_flow",
                "definition_version": "1.0.0",
                "stages": [
                    {"name": "file", "kind": "task", "agent_id": "agent-a", "retry": {"max_turns": max_turns}}
                ],
            }

        skeleton = workflow_definitions.load_skeleton_payload(payload(20))
        self.assertEqual(skeleton.stage("file").retry["max_turns"], 20)
        for bad in (0, 65, "20", True):
            with self.assertRaises(ValueError):
                workflow_definitions.load_skeleton_payload(payload(bad))


if __name__ == "__main__":
    unittest.main()
