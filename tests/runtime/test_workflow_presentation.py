"""Tests for automatic specialist artifact presentation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.config.schema import load_runtime_config
from ts_harness.agent.transcript import (
    TranscriptStore,
    get_transcript_store,
    reset_transcript_store,
)
from ts_harness.gateway.workflow_presentation import (
    _parse_source_channel,
    _session_for_workflow,
    binding_key_from_source_channel,
    _resolve_share_email,
    present_workflow_artifact,
)
from ts_harness.workflows.engine import WorkflowEngine
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.registry import HookContext

REPO_ROOT = Path(__file__).resolve().parents[2]


class WorkflowPresentationTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        transcript_file = tempfile.NamedTemporaryFile(
            prefix="workflow-present-transcript-",
            suffix=".jsonl",
            delete=False,
        )
        transcript_file.close()
        self.addCleanup(lambda: Path(transcript_file.name).unlink(missing_ok=True))
        reset_transcript_store(TranscriptStore(path=transcript_file.name))
        self.session = SessionRecord(
            session_id="slack:C123:thread-1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "workflow_id": "wf-1",
                "workflow_type": "prd_request",
                "mode": "workflow_linked",
                "channel": "slack",
                "channel_id": "C123",
                "thread_id": "thread-1",
            },
        )
        get_session_store().save(self.session)

    def test_parse_source_channel_accepts_admin_chat_binding(self):
        self.assertEqual(_parse_source_channel("web:abc-123"), ("web", "web", "abc-123"))
        self.assertEqual(
            binding_key_from_source_channel("web:abc-123"),
            "web:web:abc-123",
        )
        self.assertEqual(
            _parse_source_channel("web:web:abc-123"),
            ("web", "web", "abc-123"),
        )
        self.assertEqual(
            _parse_source_channel("web:web:web:live-bug-5"),
            ("web", "web", "live-bug-5"),
        )
        self.assertEqual(
            binding_key_from_source_channel("web:web:web:live-bug-5"),
            "web:web:live-bug-5",
        )
        self.assertIsNone(_parse_source_channel("slack-only"))

    def test_session_lookup_matches_admin_chat_after_the_workflow_changes(self):
        chat = SessionRecord(
            session_id="sess-web-1",
            routing_key="web:routing",
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
            metadata={
                "workflow_id": "wf-work-orders",
                "active_workflow_id": "wf-implement",
                "binding_key": "web:abc-123",
                "program_id": "program-1",
            },
        )
        get_session_store().save(chat)

        by_binding = _session_for_workflow("wf-other", "default", "web:abc-123")
        by_program = _session_for_workflow(
            "wf-other",
            "default",
            "web:someone-else",
            program_id="program-1",
        )
        by_active = _session_for_workflow("wf-implement", "default", "")

        self.assertEqual(by_binding.session_id, "sess-web-1")
        self.assertEqual(by_program.session_id, "sess-web-1")
        self.assertEqual(by_active.session_id, "sess-web-1")

        later = SessionRecord(
            session_id="sess-web-2",
            routing_key="web:routing-2",
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
            metadata={
                "workflow_id": "wf-bug-intake",
                "binding_key": "web:web:web:live-bug-5",
                "program_id": "program-2",
            },
        )
        get_session_store().save(later)
        by_thread = _session_for_workflow("wf-work-order", "default", "web:live-bug-5")
        by_program_only = _session_for_workflow(
            "wf-work-order",
            "default",
            "slack:C9:1",
            program_id="program-2",
        )
        self.assertEqual(by_thread.session_id, "sess-web-2")
        self.assertEqual(by_program_only.session_id, "sess-web-2")

    def test_present_finds_the_chat_by_program_when_the_workflow_changed(self):
        chat = SessionRecord(
            session_id="sess-web-3",
            routing_key="web:routing-3",
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
            metadata={
                "workflow_id": "wf-old",
                "binding_key": "slack:C1:9",
                "program_id": "program-9",
                "channel": "web",
            },
        )
        get_session_store().save(chat)
        get_transcript_store().append_messages(chat.session_id, [])
        result = present_workflow_artifact(
            workflow_id="wf-new",
            workflow_type="bug_work_order",
            tenant_id="default",
            step_name="draft_work_order",
            agent_id="pm-agent",
            present_prompt="approval",
            artifact={"work_orders_markdown": "# WO-001\n\nFix the settings page."},
            source_channel="web:live-bug-5",
            program_id="program-9",
        )
        self.assertEqual(result["status"], "presented")
        self.assertNotEqual(result.get("error"), "no linked session for workflow")

    @patch("ts_harness.gateway.workflow_presentation.get_channel_router")
    @patch(
        "ts_harness.gateway.workflow_presentation.publish_markdown_to_google_doc"
    )
    @patch(
        "ts_harness.gateway.workflow_presentation.publish_program_artifacts"
    )
    def test_present_workflow_artifact_to_admin_chat_uses_transcript_only(
        self, mock_library, mock_publish, router_getter,
    ):
        mock_library.return_value = {"status": "skipped", "reason": "test"}
        mock_publish.return_value = {"status": "skipped"}
        router = MagicMock()
        router_getter.return_value = router

        web_session = SessionRecord(
            session_id="sess-web-1",
            routing_key="rk-web",
            tenant_id="default",
            agent_id="orchestrator",
            source="web",
            metadata={
                "workflow_id": "wf-campaign",
                "workflow_type": "content_campaign",
                "mode": "workflow_linked",
                "channel": "web",
                "binding_key": "web:web:client-bind",
            },
        )
        get_session_store().save(web_session)
        get_transcript_store().append_messages(web_session.session_id, [])

        result = present_workflow_artifact(
            workflow_id="wf-campaign",
            workflow_type="content_campaign",
            tenant_id="default",
            step_name="draft_asset",
            agent_id="marketing-agent",
            present_prompt="approval",
            artifact={"markdown": "# Blog draft\n\nHello world"},
            source_channel="web:web:client-bind",
            runtime_config=load_runtime_config(
                REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
            ),
        )

        self.assertEqual(result["status"], "presented")
        self.assertEqual(result["channel"], "web")
        self.assertEqual(result["reply"], {"status": "transcript"})
        router.send.assert_not_called()
        messages = get_transcript_store().load_messages(web_session.session_id)
        self.assertTrue(any("Blog draft" in str(m.get("content") or "") for m in messages))
        self.assertTrue(any("Reply yes to approve" in str(m.get("content") or "") for m in messages))

    @patch("ts_harness.gateway.workflow_presentation.get_channel_router")
    @patch(
        "ts_harness.gateway.workflow_presentation.publish_program_artifacts"
    )
    @patch(
        "ts_harness.gateway.workflow_presentation.publish_markdown_to_google_doc"
    )
    def test_present_workflow_artifact_includes_library_links(
        self, mock_publish, mock_library, router_getter,
    ):
        mock_library.return_value = {
            "status": "published",
            "root_web_view_link": "https://drive.example/program",
            "manifest_web_view_link": "https://docs.example/manifest",
            "drive_library": {"root_folder_id": "folder-1"},
        }
        mock_publish.return_value = {"status": "skipped"}
        router = MagicMock()
        router.send.return_value = {"status": "sent"}
        router_getter.return_value = router

        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        result = present_workflow_artifact(
            workflow_id="wf-1",
            workflow_type="work_orders_from_prd",
            tenant_id="default",
            step_name="refine_requirements",
            agent_id="pm-agent",
            present_prompt="approval",
            artifact={"frds_markdown": "# FRD-001\n\nContent"},
            source_channel="slack:C123:thread-1",
            runtime_config=config,
            program_id="prog-1",
            program_summary="Online checkers",
        )

        self.assertEqual(result["status"], "presented")
        mock_library.assert_called_once()
        mock_publish.assert_not_called()
        message = router.send.call_args.args[1]["text"]
        self.assertIn("https://drive.example/program", message)
        self.assertIn("https://docs.example/manifest", message)

    @patch("ts_harness.gateway.workflow_presentation.get_channel_router")
    @patch(
        "ts_harness.gateway.workflow_presentation.publish_program_artifacts"
    )
    @patch(
        "ts_harness.gateway.workflow_presentation.publish_markdown_to_google_doc"
    )
    def test_present_workflow_artifact_posts_and_persists_handoff(
        self, mock_publish, mock_library, router_getter,
    ):
        mock_library.return_value = {"status": "skipped", "reason": "test"}
        mock_publish.return_value = {
            "status": "published",
            "webViewLink": "https://docs.example/doc-1",
            "documentId": "doc-1",
        }
        router = MagicMock()
        router.send.return_value = {"status": "sent"}
        router_getter.return_value = router

        result = present_workflow_artifact(
            workflow_id="wf-1",
            workflow_type="prd_request",
            tenant_id="default",
            step_name="draft_prd",
            agent_id="pm-agent",
            present_prompt="approval",
            artifact={
                "prd_title": "Checker Game",
                "prd_markdown": "# Overview\n\n" + ("Online checkers detail line.\n" * 200),
            },
            source_channel="slack:C123:thread-1",
            runtime_config=load_runtime_config(
                REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
            ),
            share_with_email="tester@test.local",
        )

        self.assertEqual(result["status"], "presented")
        mock_publish.assert_called_once()
        router.send.assert_called_once()
        message = router.send.call_args.args[1]["text"]
        self.assertIn("https://docs.example/doc-1", message)
        self.assertIn("Checker Game", message)
        self.assertIn("pm-agent", message)
        self.assertIn("Online checkers detail", message)
        self.assertIn("Reply yes to approve", message)

        saved = get_session_store().get("slack:C123:thread-1")
        assert saved is not None
        self.assertEqual(saved.metadata.get("workflow_phase"), "awaiting_approval")
        self.assertEqual(
            saved.metadata.get("specialist_handoff", {}).get("agent_id"),
            "pm-agent",
        )

    def test_resolve_share_email_uses_session_slack_user_not_channel_id(self):
        from ts_harness.gateway.channels.slack_users import _CACHE

        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        config.identity.slack_user_emails = {"U123ABC01": "slack-user@corp.com"}
        session = SessionRecord(
            session_id="slack:C123:thread-1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "slack_user_id": "U123ABC01",
                "binding_key": "slack:C123:thread-1",
            },
        )
        _CACHE.clear()
        email = _resolve_share_email(
            runtime_config=config,
            session=session,
            share_with_email=None,
            source_channel="slack:C123:thread-1",
            requester_actor_id="orchestrator",
        )
        self.assertEqual(email, "slack-user@corp.com")

        session_no_map = SessionRecord(
            session_id="slack:C123:thread-2",
            routing_key="rk2",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"slack_user_id": "U999ZZZ99"},
        )
        _CACHE.clear()
        email = _resolve_share_email(
            runtime_config=config,
            session=session_no_map,
            share_with_email=None,
            source_channel="slack:C123:thread-2",
            requester_actor_id="orchestrator",
        )
        self.assertIsNone(email)

    def test_resolve_share_email_prefers_session_actor_email(self):
        session = SessionRecord(
            session_id="slack:C123:thread-1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"actor_email": "direct@corp.com"},
        )
        email = _resolve_share_email(
            runtime_config=None,
            session=session,
            share_with_email=None,
            source_channel="slack:C123:thread-1",
            requester_actor_id="orchestrator",
        )
        self.assertEqual(email, "direct@corp.com")


class PresentSpecialistHookTests(unittest.TestCase):
    @patch(
        "ts_harness.workflows.hooks.present_artifact_for_review."
        "_runtime_present_specialist_output"
    )
    def test_present_hook_forwards_dispatch_output(self, mock_present):
        mock_present.return_value = {"status": "presented"}
        from ts_harness.workflows.hooks.present_artifact_for_review import handle
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton("prd_request")
        ctx = HookContext(
            workflow_id="wf-1",
            workflow_type="prd_request",
            tenant_id="default",
            task={"id": "draft-1", "step_name": "draft_prd", "agent_id": "pm-agent"},
            stage=skeleton.stage("draft_prd"),
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=MagicMock(),
            dispatch_result={"output": {"prd_markdown": "# Draft"}},
            workflow={"source_channel": "slack:C123:thread-1"},
            tasks=[
                {"id": "draft-1", "step_name": "draft_prd", "status": "succeeded"},
                {
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "status": "pending",
                    "approval_required": True,
                    "approval_status": "pending",
                    "depends_on": ["draft-1"],
                },
            ],
        )
        result = handle(ctx)
        self.assertEqual(result["status"], "presented")
        self.assertEqual(result["hook"], "present_specialist_output")
        mock_present.assert_called_once()
        self.assertEqual(mock_present.call_args.kwargs["present_prompt"], "approval")


class EnginePresentSpecialistTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.client = MagicMock()
        self.engine = WorkflowEngine(client=self.client, registry=build_default_registry())

    @patch("ts_harness.workflows.engine.load_workflow_payload")
    @patch(
        "ts_harness.workflows.hooks.present_artifact_for_review."
        "_runtime_present_specialist_output"
    )
    def test_draft_prd_success_presents_specialist_output(self, mock_present, mock_load):
        mock_present.return_value = {"status": "presented"}
        mock_load.return_value = (
            {"source_channel": "slack:C123:thread-1"},
            [],
        )
        result = self.engine.on_task_transition(
            {
                "id": "draft-1",
                "workflow_id": "wf-1",
                "workflow_type": "prd_request",
                "tenant_id": "default",
                "step_name": "draft_prd",
                "agent_id": "pm-agent",
                "status": "succeeded",
            },
            "succeeded",
            dispatch_result={"output": {"prd_markdown": "# Draft"}},
        )
        self.assertEqual(result["status"], "presented")
        mock_present.assert_called_once()

    @patch("ts_harness.workflows.engine.load_workflow_payload")
    @patch(
        "ts_harness.workflows.hooks.present_artifact_for_review."
        "_runtime_present_specialist_output"
    )
    def test_draft_prd_presentation_error_is_returned(self, mock_present, mock_load):
        mock_present.return_value = {
            "status": "error",
            "error": "GATEWAY_TOKEN is required",
        }
        mock_load.return_value = (
            {"source_channel": "slack:C123:thread-1"},
            [],
        )
        result = self.engine.on_task_transition(
            {
                "id": "draft-1",
                "workflow_id": "wf-1",
                "workflow_type": "prd_request",
                "tenant_id": "default",
                "step_name": "draft_prd",
                "agent_id": "pm-agent",
                "status": "succeeded",
            },
            "succeeded",
            dispatch_result={"output": {"prd_markdown": "# Draft"}},
        )
        assert result is not None
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["hook"], "present_specialist_output")
        self.assertIn("GATEWAY_TOKEN", result["error"])


if __name__ == "__main__":
    unittest.main()
