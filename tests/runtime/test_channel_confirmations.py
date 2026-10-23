# flake8: noqa
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.gateway.channel_confirmations import (
    classify_approval_gate_response,
    execute_confirmed_program_advance,
    execute_confirmed_program_start,
    execute_confirmed_workflow_start,
    execute_governed_workflow_proposal,
    execute_intake_response,
    execute_workflow_gate_response,
    execute_workflow_artifact_query,
    execute_workflow_status_query,
    format_intake_response_ack,
    format_program_advanced_message,
    format_program_started_message,
    format_workflow_gate_response_ack,
    format_workflow_artifact_message,
    format_workflow_proposal_message,
    format_workflow_started_message,
    format_workflow_status_message,
    is_workflow_artifact_query,
    is_workflow_artifact_provenance_query,
    is_workflow_status_query,
    should_auto_explain_workflow_artifact,
    should_auto_handle_intake_response,
    should_auto_handle_workflow_gate_response,
    should_auto_propose_workflow,
    should_auto_report_workflow_status,
    should_auto_show_workflow_artifact,
    should_intercept_approval_gate_reply,
)
from ts_harness.gateway.confirmation_classifier import (
    classify_confirmation,
    classify_confirmation_deterministic,
)
from ts_harness.agent.provider import FakeProvider
from ts_harness.gateway.delegate_confirmation import (
    resolve_program_advance_confirmation,
    resolve_program_confirmation,
    set_pending_program,
    set_pending_program_advance,
    set_pending_workflow,
)
from ts_harness.routing.intents import ChannelIntent


class ChannelConfirmationExecutionTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.session = SessionRecord(
            session_id="slack:C123:thread-1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "channel": "slack",
                "channel_id": "C123",
                "thread_id": "thread-1",
            },
        )
        get_session_store().save(self.session)

    def test_execute_confirmed_workflow_start_promotes_session(self):
        set_pending_workflow(
            self.session,
            workflow_type="prd_request",
            summary="PRD for online multiplayer checker game",
            input_data={"topic": "checkers"},
        )
        session = get_session_store().get(self.session.session_id)
        assert session is not None

        with patch("ts_harness.tools.workflow_tools.WorkflowPromotionService") as service_cls:
            service_cls.return_value.start.return_value = MagicMock(
                workflow_id="wf-1",
                workflow_type="prd_request",
                task_ids=["t1", "t2", "t3"],
                first_step="intake",
                binding_key="slack:C123:thread-1",
            )
            result = execute_confirmed_workflow_start(
                session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["workflow_id"], "wf-1")
        service_cls.return_value.start.assert_called_once()

    def test_execute_confirmed_workflow_start_without_pending_returns_none(self):
        result = execute_confirmed_workflow_start(
            self.session,
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
        )
        self.assertIsNone(result)

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_execute_confirmed_program_start_creates_program(self, service_factory):
        service = MagicMock()
        service.create_program.return_value = MagicMock(
            program_id="prog-1",
            workflow_id="wf-jira",
            workflow_type="jira_from_prd",
            entry_phase="jira",
            task_ids=["t1"],
            first_step="create_jira",
        )
        service_factory.return_value = service
        set_pending_program(
            self.session,
            entry_phase="jira",
            summary="File Jira from PRD",
            artifacts={"approved_prd": {"title": "PRD"}},
        )
        session = get_session_store().get(self.session.session_id)
        assert session is not None

        result = execute_confirmed_program_start(
            session,
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["program_id"], "prog-1")
        self.assertEqual(result["workflow_id"], "wf-jira")
        service.create_program.assert_called_once()

    @patch("ts_harness.gateway.server.get_runtime")
    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_confirmed_program_start_passes_actor_department(self, service_factory, get_runtime):
        service = MagicMock()
        service.create_program.return_value = MagicMock(
            program_id="prog-1",
            workflow_id="wf-jira",
            workflow_type="jira_from_prd",
            entry_phase="jira",
            task_ids=["t1"],
            first_step="create_jira",
        )
        service_factory.return_value = service
        get_runtime.return_value.config.identity.departments = {"ada@example.com": "engineering"}
        set_pending_program(
            self.session,
            entry_phase="jira",
            summary="File Jira from PRD",
            artifacts={"approved_prd": {"title": "PRD"}},
        )
        session = get_session_store().get(self.session.session_id)
        assert session is not None
        execute_confirmed_program_start(
            session,
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            actor_email="Ada@example.com",
        )
        self.assertEqual(service.create_program.call_args.kwargs["department"], "engineering")

    def test_execute_confirmed_program_start_without_pending_returns_none(self):
        result = execute_confirmed_program_start(
            self.session,
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
        )
        self.assertIsNone(result)

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_execute_confirmed_program_advance_advances_program(self, service_factory):
        service = MagicMock()
        service.advance_program.return_value = {
            "program_id": "prog-1",
            "target_phase": "jira",
            "workflow_id": "wf-jira",
            "workflow_type": "jira_from_prd",
            "first_step": "create_jira",
            "task_ids": ["t1"],
        }
        service_factory.return_value = service
        self.session.metadata = {
            **(self.session.metadata or {}),
            "program_id": "prog-1",
            "workflow_id": "wf-prd",
            "workflow_type": "prd_request",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": "thread-1",
        }
        get_session_store().save(self.session)
        set_pending_program_advance(
            self.session,
            program_id="prog-1",
            summary="Continue to Jira",
            target_phase="jira",
        )
        session = get_session_store().get(self.session.session_id)
        assert session is not None

        result = execute_confirmed_program_advance(
            session,
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["workflow_id"], "wf-jira")
        service.advance_program.assert_called_once()

    def test_resolve_program_confirmation_proceed(self):
        set_pending_program(
            self.session,
            entry_phase="prd",
            summary="New PRD",
        )
        session = get_session_store().get(self.session.session_id)
        assert session is not None
        result = resolve_program_confirmation(session, "yes")
        self.assertTrue(result.confirmed)

    def test_resolve_program_advance_confirmation_proceed(self):
        set_pending_program_advance(
            self.session,
            program_id="prog-1",
            summary="Continue delivery",
        )
        session = get_session_store().get(self.session.session_id)
        assert session is not None
        result = resolve_program_advance_confirmation(session, "proceed")
        self.assertTrue(result.confirmed)

    def test_format_program_started_message(self):
        message = format_program_started_message(
            {
                "entry_phase": "jira",
                "workflow_type": "jira_from_prd",
                "first_step": "create_jira",
            }
        )
        self.assertIn("jira", message.lower())
        self.assertIn("create jira", message.lower())

    def test_format_program_advanced_message(self):
        message = format_program_advanced_message(
            {
                "target_phase": "jira",
                "workflow_type": "jira_from_prd",
                "first_step": "create_jira",
            }
        )
        self.assertIn("advanced", message.lower())
        self.assertIn("jira", message.lower())

    def test_format_workflow_started_message(self):
        message = format_workflow_started_message(
            {"workflow_type": "prd_request", "first_step": "intake"}
        )
        self.assertIn("prd request", message.lower())
        self.assertIn("intake", message.lower())

    def test_should_auto_propose_for_governed_prd_intent(self):
        intent = ChannelIntent(
            kind="governed_prd",
            workflow_type="prd_request",
            summary="PRD for checker game",
        )
        self.assertTrue(should_auto_propose_workflow(intent, {}))
        self.assertFalse(
            should_auto_propose_workflow(
                intent,
                {"workflow_id": "wf-1", "mode": "workflow_linked"},
            )
        )
        self.assertTrue(
            should_auto_propose_workflow(
                intent,
                {"pending_workflow": {"workflow_type": "prd_request"}},
            )
        )

    def test_auto_propose_sets_pending_workflow(self):
        intent = ChannelIntent(
            kind="governed_prd",
            workflow_type="prd_request",
            summary="PRD for checker game",
        )
        self.assertTrue(should_auto_propose_workflow(intent, {}))
        result = execute_governed_workflow_proposal(
            self.session,
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            workflow_type="prd_request",
            summary="PRD for checker game",
        )
        self.assertEqual(result.get("status"), "awaiting_confirmation")
        saved = get_session_store().get(self.session.session_id)
        assert saved is not None
        self.assertEqual(saved.metadata.get("pending_workflow", {}).get("workflow_type"), "prd_request")

    def test_format_workflow_proposal_message(self):
        message = format_workflow_proposal_message(
            {
                "workflow_type": "prd_request",
                "summary": "online multiplayer checker game",
            }
        )
        self.assertIn("prd request", message.lower())
        self.assertIn("online multiplayer checker game", message)
        self.assertIn("yes", message.lower())

    def test_format_workflow_proposal_message_shortens_long_summary(self):
        long_summary = (
            "Build a crypto portfolio dashboard that tracks wallets, taxes, and alerts. "
            + ("Include holdings, performance, and export. " * 30)
        )
        message = format_workflow_proposal_message(
            {
                "workflow_type": "prd_request",
                "summary": long_summary,
            }
        )
        self.assertIn("I've prepared a prd request workflow for:", message)
        self.assertIn("Build a crypto portfolio dashboard", message)
        self.assertNotIn(long_summary, message)
        self.assertLess(len(message), len(long_summary))
        self.assertIn("Reply yes to start, or no to cancel.", message)

    def test_is_workflow_status_query_matches_common_phrases(self):
        self.assertTrue(is_workflow_status_query("okay, whats the status?"))
        self.assertTrue(is_workflow_status_query("what's the status"))
        self.assertTrue(is_workflow_status_query("workflow status"))
        self.assertFalse(is_workflow_status_query("yes"))
        self.assertFalse(is_workflow_status_query("Create a PRD for checkers"))

    def test_should_auto_report_workflow_status_requires_linked_or_pending(self):
        metadata = {
            "workflow_id": "wf-1",
            "mode": "workflow_linked",
            "workflow_type": "prd_request",
        }
        self.assertTrue(should_auto_report_workflow_status(metadata, "what's the status?"))
        self.assertFalse(should_auto_report_workflow_status({}, "what's the status?"))

    def test_format_workflow_status_message_lists_task_states(self):
        message = format_workflow_status_message(
            {
                "workflow_type": "prd_request",
                "status": "running",
                "tasks": [
                    {"id": "t1", "step_name": "intake", "status": "succeeded"},
                    {"id": "t2", "step_name": "draft_prd", "status": "running"},
                    {
                        "id": "t3",
                        "step_name": "approve_prd",
                        "status": "pending",
                        "approval_required": True,
                        "approval_status": "pending",
                        "depends_on": ["t2"],
                    },
                ],
            }
        )
        self.assertIn("intake: succeeded", message)
        self.assertIn("draft prd: running", message)
        self.assertIn("approve prd: pending", message)
        self.assertNotIn("awaiting your approval", message)

    def test_format_workflow_status_message_shows_actionable_gate(self):
        message = format_workflow_status_message(
            {
                "workflow_type": "prd_request",
                "status": "running",
                "tasks": [
                    {"id": "t1", "step_name": "intake", "status": "succeeded"},
                    {"id": "t2", "step_name": "draft_prd", "status": "succeeded"},
                    {
                        "id": "t3",
                        "step_name": "approve_prd",
                        "status": "pending",
                        "approval_required": True,
                        "approval_status": "pending",
                        "depends_on": ["t2"],
                    },
                ],
            }
        )
        self.assertIn("awaiting your approval", message)
        self.assertIn("Reply yes to approve", message)

    def test_should_auto_handle_workflow_gate_response_accepts_approved(self):
        tasks = [
            {"id": "t1", "step_name": "draft_prd", "status": "succeeded"},
            {
                "id": "gate-1",
                "step_name": "approve_prd",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "depends_on": ["t1"],
            },
        ]
        self.assertTrue(
            should_auto_handle_workflow_gate_response(
                {"workflow_id": "wf-1"},
                "approved",
                tasks=tasks,
            )
        )

    def test_execute_workflow_gate_response_approves_current_gate(self):
        self.session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
        }
        with patch("ts_harness.workflows.engine.get_workflow_engine") as engine_cls:
            engine = engine_cls.return_value
            engine.get_status.return_value = {
                "tasks": [
                    {
                        "id": "draft-1",
                        "step_name": "draft_prd",
                        "status": "succeeded",
                    },
                    {
                        "id": "gate-1",
                        "workflow_id": "wf-1",
                        "workflow_type": "prd_request",
                        "step_name": "approve_prd",
                        "status": "pending",
                        "approval_required": True,
                        "approval_status": "pending",
                        "depends_on": ["draft-1"],
                    },
                ],
            }
            engine.handle_approve.return_value = {
                "id": "gate-1",
                "approval_status": "approved",
            }

            result = execute_workflow_gate_response(
                self.session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
                user_text="approved",
            )

        engine.handle_approve.assert_called_once()
        self.assertEqual(result["status"], "approved")
        self.assertIn("approved", format_workflow_gate_response_ack(result).lower())

    def test_classify_approval_gate_response_looks_good(self):
        result = classify_approval_gate_response("looks good", use_llm_fallback=False)
        self.assertEqual(result.decision, "approve")
        self.assertEqual(result.source, "deterministic")

    def test_classify_approval_gate_response_lgtm(self):
        result = classify_approval_gate_response("lgtm", use_llm_fallback=False)
        self.assertEqual(result.decision, "approve")

    def test_classify_approval_gate_response_not_yet_with_feedback(self):
        result = classify_approval_gate_response(
            "not yet, add audio chat",
            use_llm_fallback=False,
        )
        self.assertEqual(result.decision, "reject")
        self.assertEqual(result.feedback, "add audio chat")

    def test_classify_approval_gate_response_maybe_is_ambiguous(self):
        result = classify_approval_gate_response("maybe", use_llm_fallback=False)
        self.assertEqual(result.decision, "ambiguous")

    def test_classify_approval_gate_response_more_verbose_is_reject(self):
        result = classify_approval_gate_response(
            "Can you make the draft more verbose",
            use_llm_fallback=False,
        )
        self.assertEqual(result.decision, "reject")
        self.assertIn("verbose", result.feedback.lower())

    def test_format_workflow_gate_response_ack_includes_bridge_cta(self):
        ack = format_workflow_gate_response_ack(
            {"status": "approved"},
            workflow_type="prd_request",
        )
        self.assertIn("work orders", ack.lower())
        self.assertIn("jira", ack.lower())

    def test_should_auto_explain_workflow_artifact_provenance(self):
        metadata = {
            "workflow_id": "wf-1",
            "specialist_handoff": {
                "step_name": "draft_prd",
                "agent_id": "pm-agent",
                "output": {"result": {"prd_markdown": "# PRD"}},
            },
        }
        self.assertTrue(
            should_auto_explain_workflow_artifact(metadata, "is this from draft_prd?")
        )
        self.assertTrue(is_workflow_artifact_provenance_query("where did this come from?"))

    def test_should_auto_handle_workflow_gate_response_accepts_looks_good(self):
        tasks = [
            {"id": "t1", "step_name": "draft_prd", "status": "succeeded"},
            {
                "id": "gate-1",
                "step_name": "approve_prd",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "depends_on": ["t1"],
            },
        ]
        self.assertTrue(
            should_auto_handle_workflow_gate_response(
                {"workflow_id": "wf-1"},
                "looks good",
                tasks=tasks,
            )
        )

    def test_should_intercept_approval_gate_reply_for_ambiguous_text(self):
        tasks = [
            {"id": "t1", "step_name": "draft_prd", "status": "succeeded"},
            {
                "id": "gate-1",
                "step_name": "approve_prd",
                "status": "pending",
                "approval_required": True,
                "approval_status": "pending",
                "depends_on": ["t1"],
            },
        ]
        self.assertTrue(
            should_intercept_approval_gate_reply(
                {"workflow_id": "wf-1"},
                "maybe",
                tasks=tasks,
            )
        )
        self.assertFalse(
            should_auto_handle_workflow_gate_response(
                {"workflow_id": "wf-1"},
                "maybe",
                tasks=tasks,
            )
        )

    def test_execute_workflow_gate_response_rejects_with_feedback(self):
        self.session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
        }
        with patch("ts_harness.workflows.engine.get_workflow_engine") as engine_cls:
            engine = engine_cls.return_value
            engine.get_status.return_value = {
                "tasks": [
                    {"id": "draft-1", "step_name": "draft_prd", "status": "succeeded"},
                    {
                        "id": "gate-1",
                        "workflow_id": "wf-1",
                        "workflow_type": "prd_request",
                        "step_name": "approve_prd",
                        "status": "pending",
                        "approval_required": True,
                        "approval_status": "pending",
                        "depends_on": ["draft-1"],
                    },
                ],
            }
            engine.handle_reject.return_value = {
                "id": "gate-1",
                "approval_status": "rejected",
            }

            result = execute_workflow_gate_response(
                self.session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
                user_text="not yet, add audio chat",
                use_llm_fallback=False,
            )

        engine.handle_reject.assert_called_once()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            engine.handle_reject.call_args.kwargs["comments"],
            "add audio chat",
        )

    def test_execute_workflow_gate_response_revision_phrase_routes_to_reject(self):
        self.session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
        }
        with patch("ts_harness.workflows.engine.get_workflow_engine") as engine_cls:
            engine = engine_cls.return_value
            engine.get_status.return_value = {
                "tasks": [
                    {"id": "draft-1", "step_name": "draft_prd", "status": "succeeded"},
                    {
                        "id": "gate-1",
                        "workflow_id": "wf-1",
                        "workflow_type": "prd_request",
                        "step_name": "approve_prd",
                        "status": "pending",
                        "approval_required": True,
                        "approval_status": "pending",
                        "depends_on": ["draft-1"],
                    },
                ],
            }
            engine.handle_reject.return_value = {
                "id": "gate-1",
                "approval_status": "rejected",
            }

            result = execute_workflow_gate_response(
                self.session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
                user_text="Can you make the draft more verbose",
                use_llm_fallback=False,
            )

        engine.handle_reject.assert_called_once()
        self.assertEqual(result["status"], "rejected")
        self.assertIn("verbose", engine.handle_reject.call_args.kwargs["comments"].lower())

    def test_execute_workflow_gate_response_ambiguous_returns_clarification(self):
        self.session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
        }
        result = execute_workflow_gate_response(
            self.session,
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            user_text="maybe",
            use_llm_fallback=False,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("approve this PRD", result["summary"])

    def test_llm_classifier_fallback_for_gate(self):
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"decision":"approve","feedback":""}',
                            }
                        }
                    ]
                }
            ]
        )
        result = classify_confirmation(
            "sounds reasonable to me",
            context="approval_gate",
            provider=provider,
        )
        self.assertEqual(result.decision, "approve")
        self.assertEqual(result.source, "llm")
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(provider.calls[0]["tools"])

    def test_deterministic_workflow_start_context(self):
        approved = classify_confirmation_deterministic("go ahead", context="workflow_start")
        rejected = classify_confirmation_deterministic("no thanks", context="workflow_start")
        self.assertEqual(approved.decision, "approve")
        self.assertEqual(rejected.decision, "reject")

    def test_deterministic_does_not_treat_mention_prefixed_approve_as_approve(self):
        result = classify_confirmation_deterministic(
            "<@U123> approve",
            context="workflow_start",
        )
        self.assertEqual(result.decision, "none")

    def test_format_workflow_status_message_notes_queued_work(self):
        message = format_workflow_status_message(
            {
                "workflow_type": "prd_request",
                "status": "running",
                "tasks": [
                    {"id": "t1", "step_name": "intake", "status": "pending"},
                    {"id": "t2", "step_name": "draft_prd", "status": "pending", "depends_on": ["t1"]},
                ],
            }
        )
        self.assertIn("still queued", message.lower())
        self.assertNotIn("awaiting your approval", message)

    def test_format_workflow_status_message_for_pending_proposal(self):
        message = format_workflow_status_message(
            {
                "status": "awaiting_confirmation",
                "workflow_type": "prd_request",
                "summary": "checker game",
            }
        )
        self.assertIn("not started yet", message.lower())
        self.assertIn("checker game", message)

    def test_execute_workflow_status_query_uses_linked_workflow(self):
        self.session.metadata["workflow_id"] = "wf-1"
        self.session.metadata["mode"] = "workflow_linked"
        get_session_store().save(self.session)
        session = get_session_store().get(self.session.session_id)
        assert session is not None

        with patch("ts_harness.tools.workflow_tools.get_workflow_engine") as engine_cls:
            engine_cls.return_value.get_status.return_value = {
                "workflow_id": "wf-1",
                "workflow_type": "prd_request",
                "status": "running",
                "tasks": [{"step_name": "intake", "status": "pending"}],
            }
            result = execute_workflow_status_query(
                session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
            )

        self.assertEqual(result["workflow_id"], "wf-1")
        engine_cls.return_value.get_status.assert_called_once_with("wf-1")

    def test_is_workflow_artifact_query_matches_common_phrases(self):
        self.assertTrue(is_workflow_artifact_query("where's the PRD?"))
        self.assertTrue(is_workflow_artifact_query("show me the draft"))
        self.assertFalse(is_workflow_artifact_query("what's the status?"))
        self.assertFalse(is_workflow_artifact_query("yes"))

    def test_should_auto_show_workflow_artifact_requires_linked_workflow(self):
        metadata = {"workflow_id": "wf-1", "mode": "workflow_linked"}
        self.assertTrue(should_auto_show_workflow_artifact(metadata, "where's the PRD?"))
        self.assertFalse(should_auto_show_workflow_artifact({}, "where's the PRD?"))

    def test_format_workflow_artifact_message_renders_prd_markdown(self):
        message = format_workflow_artifact_message(
            {
                "display_name": "PRD review",
                "artifact_step": "draft_prd",
                "artifact": {
                    "prd_title": "Online Multiplayer Checker Game",
                    "prd_markdown": "# Overview\n\nReal-time checkers.",
                },
                "pending_approvals": [{"id": "gate-1"}],
            }
        )
        self.assertIn("Online Multiplayer Checker Game", message)
        self.assertIn("Real-time checkers.", message)
        self.assertIn("Reply yes to approve", message)

    def test_format_workflow_artifact_message_notes_in_progress(self):
        message = format_workflow_artifact_message(
            {
                "display_name": "PRD review",
                "artifact_steps": ["draft_prd"],
                "tasks": [
                    {"step_name": "intake", "status": "succeeded"},
                    {"step_name": "draft_prd", "status": "running"},
                ],
            }
        )
        self.assertIn("still being prepared", message.lower())

    def test_execute_workflow_artifact_query_uses_linked_workflow(self):
        self.session.metadata["workflow_id"] = "wf-1"
        self.session.metadata["mode"] = "workflow_linked"
        self.session.metadata["workflow_type"] = "prd_request"
        get_session_store().save(self.session)
        session = get_session_store().get(self.session.session_id)
        assert session is not None

        with patch("ts_harness.tools.workflow_tools.get_workflow_engine") as engine_cls:
            engine_cls.return_value.get_status.return_value = {
                "workflow_id": "wf-1",
                "workflow_type": "prd_request",
                "status": "running",
                "tasks": [
                    {
                        "id": "draft-1",
                        "step_name": "draft_prd",
                        "status": "succeeded",
                        "created_at": "2026-01-01",
                        "output": {"prd_markdown": "# Draft"},
                    }
                ],
                "pending_approvals": [],
            }
            result = execute_workflow_artifact_query(
                session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
            )

        self.assertEqual(result["artifact"]["prd_markdown"], "# Draft")
        self.assertEqual(result["artifact_step"], "draft_prd")


class IntakeResponseTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.session = SessionRecord(
            session_id="slack:C123:thread-2",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "channel": "slack",
                "channel_id": "C123",
                "thread_id": "thread-2",
                "workflow_id": "wf-1",
                "mode": "workflow_linked",
                "pending_workflow": {"summary": "Checker game PRD"},
            },
        )
        get_session_store().save(self.session)

    def test_should_auto_handle_intake_response_when_awaiting(self):
        tasks = [
            {"step_name": "intake", "status": "succeeded"},
            {
                "id": "wait-1",
                "step_name": "await_intake_answers",
                "status": "blocked",
                "error": "awaiting_external:channel.intake_response",
            },
        ]
        self.assertTrue(
            should_auto_handle_intake_response(
                self.session.metadata or {},
                "Web and mobile",
                tasks=tasks,
            )
        )

    def test_should_auto_handle_intake_response_when_wait_setup_failed(self):
        tasks = [
            {"step_name": "intake", "status": "succeeded"},
            {
                "id": "wait-1",
                "step_name": "await_intake_answers",
                "status": "blocked",
                "error": "wait_external setup failed: memory api unavailable",
            },
        ]
        self.assertTrue(
            should_auto_handle_intake_response(
                self.session.metadata or {},
                "looks good",
                tasks=tasks,
            )
        )

    def test_should_auto_handle_intake_response_when_wait_task_not_blocked_yet(self):
        tasks = [
            {"step_name": "intake", "status": "succeeded"},
            {
                "id": "wait-1",
                "step_name": "await_intake_answers",
                "status": "pending",
                "error": "",
            },
        ]
        self.assertTrue(
            should_auto_handle_intake_response(
                self.session.metadata or {},
                "Casual gamers\nWeb\nChat and leaderboards",
                tasks=tasks,
            )
        )

    def test_should_not_auto_handle_intake_when_not_waiting(self):
        tasks = [{"step_name": "draft_prd", "status": "running"}]
        self.assertFalse(
            should_auto_handle_intake_response(
                self.session.metadata or {},
                "Web and mobile",
                tasks=tasks,
            )
        )

    def test_execute_intake_response_resumes_workflow(self):
        session = get_session_store().get(self.session.session_id)
        assert session is not None
        with patch("ts_harness.workflows.engine.get_workflow_engine") as engine_cls:
            engine_cls.return_value.get_status.return_value = {
                "workflow_id": "wf-1",
                "tasks": [
                    {
                        "step_name": "intake",
                        "status": "succeeded",
                        "output": {"questions": ["Platform?"]},
                    },
                    {
                        "id": "wait-1",
                        "step_name": "await_intake_answers",
                        "status": "blocked",
                        "error": "awaiting_external:channel.intake_response",
                    },
                ],
            }
            engine_cls.return_value.resume_external.return_value = {
                "workflow_id": "wf-1",
                "task_id": "wait-1",
                "status": "running",
            }
            result = execute_intake_response(
                session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
                user_text="Web and mobile",
            )
        engine_cls.return_value.resume_external.assert_called_once()
        self.assertEqual(result["workflow_id"], "wf-1")
        self.assertNotIn("error", result)

    def test_execute_intake_response_recovers_failed_wait_setup(self):
        session = get_session_store().get(self.session.session_id)
        assert session is not None
        with patch("ts_harness.workflows.engine.get_workflow_engine") as engine_cls:
            engine = engine_cls.return_value
            engine.get_status.return_value = {
                "workflow_id": "wf-1",
                "tasks": [
                    {
                        "step_name": "intake",
                        "status": "succeeded",
                        "output": {"questions": ["Confirm?"]},
                    },
                    {
                        "id": "wait-1",
                        "workflow_id": "wf-1",
                        "workflow_type": "prd_request",
                        "step_name": "await_intake_answers",
                        "status": "blocked",
                        "error": "wait_external setup failed: memory api unavailable",
                    },
                ],
            }
            engine.client.resume_external.return_value = {
                "status": "resumed",
                "task_id": "wait-1",
            }
            engine.client.get_workflow.return_value = {
                "tasks": [
                    {
                        "id": "wait-1",
                        "workflow_id": "wf-1",
                        "workflow_type": "prd_request",
                        "step_name": "await_intake_answers",
                        "status": "succeeded",
                        "output": {"user_response": "looks good"},
                    },
                ],
            }
            result = execute_intake_response(
                session,
                tenant_id="default",
                agent_id="orchestrator",
                source="slack",
                user_text="looks good",
            )

        engine.resume_external.assert_not_called()
        engine.client.resume_external.assert_called_once()
        engine.client.complete_task_api.assert_not_called()
        engine.on_task_transition.assert_called_once()
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(result["task_id"], "wait-1")

    def test_format_intake_response_ack(self):
        self.assertIn("queued the PRD draft", format_intake_response_ack({}))
        self.assertIn(
            "couldn't record",
            format_intake_response_ack({"error": "not waiting"}).lower(),
        )


if __name__ == "__main__":
    unittest.main()
