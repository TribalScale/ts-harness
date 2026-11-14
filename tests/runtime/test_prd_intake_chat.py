"""A PRD request confirmed in chat: the attachments reach gather_sources and intake shows every question.

Found in a live run on a deployed host: the promote hook folded intake_sources into the intake
task's context (where gather_intake_sources never looks) and stored them as program artifacts,
and the chat showed only the first question, cut before its recommended answer.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.agent.sessions import SessionRecord, SessionStore, reset_session_store
from ts_harness.intake.gather import _pointers
from ts_harness.workflows.artifacts import format_artifact_for_channel
from ts_harness.workflows.engine import WorkflowEngine
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.presentation import stage_presentation

POINTER = {"kind": "stored", "name": "brief.md", "mime": "text/markdown", "size": 10,
           "sha256": "ab" * 32, "object_key": "intake/default/ab/" + "ab" * 32}
QUESTIONS = [
    "Should only delivery managers see the missing list? Recommended: yes, managers only, because it names people.",
    "Is a Slack DM enough without a digest? Recommended: yes; a digest can follow.",
    "Should contractors be able to snooze? Recommended: no, not in the first release.",
]


class PromotedAttachmentsTests(unittest.TestCase):
    def setUp(self):
        reset_session_store(SessionStore())
        self.addCleanup(reset_session_store, SessionStore())
        self.client = MagicMock()
        self.client.create_workflow.return_value = {"id": "wf-1"}
        self.client.create_task.side_effect = [{"id": f"t{i}"} for i in range(10)]
        session = SessionRecord(
            session_id="s-1", routing_key="rk-intake-chat", tenant_id="default", agent_id="orchestrator",
            source="web", metadata={"channel": "web", "channel_id": "web", "thread_id": "web:1"},
        )
        WorkflowEngine(client=self.client).start_promotion(
            session=session, workflow_type="prd_request", summary="Write a PRD for the brief",
            tenant_id="default", binding_key="web:web:web:1",
            extra_input={"intake_sources": [POINTER], "requested_by": "pm"},
        )
        self.tasks = {call.args[0]["step_name"]: call.args[0] for call in self.client.create_task.call_args_list}

    def test_the_intake_task_carries_the_files_where_gather_reads_them(self):
        intake_input = self.tasks["intake"]["input"]
        self.assertEqual(intake_input["intake_sources"], [POINTER])
        self.assertNotIn("intake_sources", intake_input["context"])
        rows = [{**body, "created_at": str(i), "status": "pending"} for i, body in enumerate(self.tasks.values())]
        gather = self.tasks["gather_sources"]
        config = {"intake_sources": "$workflow.intake_sources"}
        self.assertEqual(_pointers(gather, config, {}, rows), [POINTER])

    def test_files_are_never_program_artifacts(self):
        artifacts = self.client.create_program.call_args.kwargs["artifacts"]
        self.assertEqual(artifacts, {"requested_by": "pm"})

    def test_gather_still_finds_files_on_workflows_promoted_before_the_fix(self):
        old_intake = {"step_name": "intake", "created_at": "1", "status": "succeeded",
                      "input": {"summary": "x", "context": {"summary": "x", "intake_sources": [POINTER]}}}
        gather = {"step_name": "gather_sources", "created_at": "0", "input": {}}
        self.assertEqual(_pointers(gather, {"intake_sources": "$workflow.intake_sources"}, {}, [gather, old_intake]),
                         [POINTER])


class IntakeQuestionsInChatTests(unittest.TestCase):
    def _message(self, output, presentation):
        return format_artifact_for_channel(output, display_name="PRD review", present_prompt="intake",
                                           step_name="intake", agent_id="pm-agent", presentation=presentation)

    def test_prd_intake_shows_every_question_with_its_recommendation(self):
        presentation = stage_presentation(load_skeleton("prd_request"), "intake")
        self.assertEqual(presentation.get("questions"), "all")
        message = self._message({"summary": 'A few decisions. Reply "yes" to accept them.', "questions": QUESTIONS},
                                presentation)
        for index, question in enumerate(QUESTIONS, start=1):
            self.assertIn(f"{index}. {question}", message)
        self.assertIn("A few decisions.", message)
        self.assertIn('Reply "yes" to accept the recommended answers', message)

    def test_a_plain_text_reply_is_not_shown_twice(self):
        text = "Before drafting:\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(QUESTIONS, start=1))
        message = self._message({"summary": text, "questions": QUESTIONS}, {"questions": "all"})
        self.assertEqual(message.count(QUESTIONS[0]), 1)

    def test_other_intakes_still_ask_one_question_at_a_time(self):
        message = self._message({"summary": "", "questions": ["What broke?", "When did it start?"]}, {})
        self.assertIn("What broke?", message)
        self.assertNotIn("When did it start?", message)
        self.assertIn("Say ready when you want a draft.", message)

    def test_definitions_reject_an_unknown_question_mode(self):
        from workflow_definitions import freeze_contract

        frozen = freeze_contract({"output_schema": {"type": "object"}, "presentation": {"questions": "all"}})
        self.assertEqual(frozen["presentation"], {"questions": "all"})
        from workflow_definitions import _contract_to_payload

        self.assertEqual(_contract_to_payload(frozen)["presentation"], {"questions": "all"})
        with self.assertRaisesRegex(ValueError, "presentation.questions must be one of"):
            freeze_contract({"output_schema": {"type": "object"}, "presentation": {"questions": "some"}})


if __name__ == "__main__":
    unittest.main()


class ApprovedPrdArtifactTests(unittest.TestCase):
    """The PRD phase records the approved draft on the program, not only on the chat session."""

    def _tasks(self, gate_status="succeeded", approval_status="approved"):
        return [
            {"step_name": "draft_prd", "status": "succeeded", "created_at": "1",
             "output": {"result": {"prd_markdown": "# PRD: v1"}}},
            {"step_name": "revise_prd", "status": "succeeded", "created_at": "2",
             "output": {"result": {"prd_markdown": "# PRD: v2"}, "prd_checks": []}},
            {"step_name": "approve_prd", "status": gate_status, "created_at": "3", "approval_required": True,
             "approval_status": approval_status, "output": {"summary": "approved"}},
        ]

    def test_the_newest_approved_draft_becomes_approved_prd(self):
        from ts_harness.workflows.program_adapters import _extract_prd_outputs

        patch = _extract_prd_outputs(workflow_type="prd_request", tasks=self._tasks())
        self.assertEqual(patch["approved_prd"]["result"]["prd_markdown"], "# PRD: v2")

    def test_nothing_is_recorded_before_the_gate_passes(self):
        from ts_harness.workflows.program_adapters import _extract_prd_outputs

        self.assertEqual(_extract_prd_outputs(workflow_type="prd_request", tasks=self._tasks("pending", "pending")), {})

    def test_an_explicit_approved_prd_output_still_wins(self):
        from ts_harness.workflows.program_adapters import _extract_prd_outputs

        tasks = self._tasks() + [{"step_name": "x", "status": "succeeded", "created_at": "4",
                                  "output": {"approved_prd": {"title": "explicit"}}}]
        self.assertEqual(_extract_prd_outputs(workflow_type="prd_request", tasks=tasks)["approved_prd"],
                         {"title": "explicit"})
