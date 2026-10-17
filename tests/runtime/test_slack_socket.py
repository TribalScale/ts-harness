"""Tests for Slack Socket Mode listener helpers and health wiring."""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from ts_harness.agent.sessions import (
    SessionRecord,
    get_session_store,
    reset_session_store,
)
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.channels.slack import SlackAdapter
from ts_harness.gateway.channels.slack_socket import (
    SlackSocketListener,
    build_envelope,
    build_reaction_event,
    compute_backoff,
    is_non_recoverable_auth_error,
    should_process_message,
    should_process_reaction,
    socket_mode_enabled,
)
from ts_harness.gateway.health import build_health_report
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.workflow_tools import propose_start_workflow_handler


class SlackSocketHelperTests(unittest.TestCase):
    def test_build_envelope_matches_adapter_unwrap(self):
        body = {"event_id": "E1", "team_id": "T1"}
        event = {"user": "U1", "text": "hello", "ts": "1.1", "channel": "C1"}
        envelope = build_envelope(body, event)
        self.assertEqual(envelope["dedup_key"], "1.1")
        normalized = SlackAdapter().normalize_inbound(envelope, tenant_id="default")
        self.assertEqual(normalized.text, "hello")
        self.assertEqual(normalized.sender_id, "U1")

    def test_build_envelope_dedup_key_uses_client_msg_id(self):
        event = {
            "user": "U1",
            "text": "hello",
            "ts": "1.1",
            "client_msg_id": "abc-123",
            "channel": "C1",
        }
        message_envelope = build_envelope({"event_id": "EvMsg", "team_id": "T1"}, event)
        mention_envelope = build_envelope({"event_id": "EvMention", "team_id": "T1"}, event)
        self.assertEqual(message_envelope["dedup_key"], "abc-123")
        self.assertEqual(mention_envelope["dedup_key"], "abc-123")

        adapter = SlackAdapter()
        first = adapter.normalize_inbound(message_envelope, tenant_id="default")
        self.assertEqual(first.text, "hello")
        with self.assertRaises(ValueError):
            adapter.normalize_inbound(mention_envelope, tenant_id="default")

    def test_reaction_add_and_remove_are_not_deduped(self):
        base_event = {
            "user": "U1",
            "reaction": "eyes",
            "item": {"type": "message", "channel": "C1", "ts": "9.9"},
        }
        added = build_reaction_event(base_event, action="reaction_added")
        removed = build_reaction_event(base_event, action="reaction_removed")
        self.assertNotEqual(added["dedup_key"], removed["dedup_key"])

        adapter = SlackAdapter()
        adapter.normalize_inbound(added, tenant_id="default")
        adapter.normalize_inbound(removed, tenant_id="default")

    def test_normalize_inbound_dedup_is_atomic_under_concurrency(self):
        adapter = SlackAdapter()
        envelope = build_envelope(
            {"event_id": "Ev1", "team_id": "T1"},
            {"user": "U1", "text": "race", "ts": "42.0", "channel": "C1"},
        )
        results: list[str | None] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            try:
                adapter.normalize_inbound(envelope, tenant_id="default")
                results.append("ok")
            except ValueError as exc:
                results.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), ["duplicate slack event", "ok"])

    def test_should_process_message_dm(self):
        self.assertTrue(
            should_process_message(
                {"channel_type": "im", "text": "hello"},
                bot_user_id="B1",
                require_mention=True,
            )
        )

    def test_should_process_message_channel_without_mention_rejected(self):
        self.assertFalse(
            should_process_message(
                {"channel_type": "channel", "text": "hello everyone"},
                bot_user_id="B1",
                require_mention=True,
            )
        )

    def test_should_process_message_channel_with_mention_deferred_to_app_mention(self):
        self.assertFalse(
            should_process_message(
                {"channel_type": "channel", "text": "hello <@B1>"},
                bot_user_id="B1",
                require_mention=True,
            )
        )
        self.assertTrue(
            should_process_message(
                {"channel_type": "channel", "text": "hello <@B1>"},
                bot_user_id="B1",
                require_mention=True,
                from_app_mention=True,
            )
        )

    def test_should_process_message_app_mention(self):
        self.assertTrue(
            should_process_message(
                {"channel_type": "channel", "text": "hello"},
                bot_user_id="B1",
                require_mention=True,
                from_app_mention=True,
            )
        )

    def test_should_process_message_bot_loop_rejected(self):
        self.assertFalse(
            should_process_message(
                {"bot_id": "B99", "text": "loop"},
                bot_user_id="B1",
                require_mention=False,
            )
        )

    def test_should_process_message_ignored_subtype(self):
        self.assertFalse(
            should_process_message(
                {"subtype": "message_changed", "text": "edit"},
                bot_user_id="B1",
                require_mention=False,
            )
        )

    def test_reaction_modes(self):
        event = {
            "user": "U1",
            "item_user": "B1",
            "item": {"type": "message", "channel": "C1", "ts": "1.0"},
            "reaction": "thumbsup",
        }
        self.assertFalse(
            should_process_reaction(event, reaction_mode="off", bot_user_id="B1")
        )
        self.assertTrue(
            should_process_reaction(event, reaction_mode="own", bot_user_id="B1")
        )
        self.assertFalse(
            should_process_reaction(
                {**event, "item_user": "U9"},
                reaction_mode="own",
                bot_user_id="B1",
            )
        )
        self.assertTrue(
            should_process_reaction(event, reaction_mode="all", bot_user_id="B1")
        )

    def test_build_reaction_event(self):
        envelope = build_reaction_event(
            {
                "user": "U1",
                "reaction": "eyes",
                "item": {"type": "message", "channel": "C1", "ts": "9.9"},
            },
            action="reaction_added",
        )
        self.assertEqual(envelope["type"], "event_callback")
        self.assertIn("reaction_added", envelope["event"]["text"])

    def test_non_recoverable_auth_error(self):
        self.assertTrue(is_non_recoverable_auth_error("token_revoked"))
        self.assertFalse(is_non_recoverable_auth_error("temporary network issue"))

    def test_compute_backoff_increases(self):
        first = compute_backoff(1)
        second = compute_backoff(3)
        self.assertLess(first, second)

    def test_socket_mode_enabled_requires_tokens(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(socket_mode_enabled())
        with mock.patch.dict(
            os.environ,
            {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"},
            clear=True,
        ):
            self.assertTrue(socket_mode_enabled())
        with mock.patch.dict(
            os.environ,
            {
                "SLACK_BOT_TOKEN": "xoxb-1",
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_SOCKET_MODE": "false",
            },
            clear=True,
        ):
            self.assertFalse(socket_mode_enabled())
        with mock.patch.dict(
            os.environ,
            {
                "SLACK_BOT_TOKEN": "xoxb-1",
                "SLACK_APP_TOKEN": "xapp-1",
                "SLACK_MODE": "http",
            },
            clear=True,
        ):
            self.assertFalse(socket_mode_enabled())


class SlackSocketHealthTests(unittest.TestCase):
    def test_health_report_includes_connected_socket(self):
        report = build_health_report(slack_socket="connected")
        payload = report.to_dict()
        self.assertEqual(payload["components"]["slack_socket"]["status"], "ok")

    def test_health_report_omits_disabled_socket(self):
        report = build_health_report()
        payload = report.to_dict()
        self.assertNotIn("slack_socket", payload["components"])

    def test_listener_health_disabled_without_tokens(self):
        runtime = mock.Mock()
        runtime.config.gateway.default_tenant_id = "default"
        with mock.patch.dict(os.environ, {}, clear=True):
            listener = SlackSocketListener(runtime)
            listener.start()
            health = listener.health()
        self.assertEqual(health.status, "disabled")

    def test_safe_handle_delegates_to_runtime(self):
        runtime = mock.Mock()
        runtime.config.gateway.default_tenant_id = "default"
        listener = SlackSocketListener(runtime)
        auth = mock.Mock(tenant_id="default", actor_id="slack:U1")
        envelope = build_envelope({"event_id": "E1"}, {"user": "U1", "text": "hi", "ts": "1"})
        listener._safe_handle(envelope, auth)
        runtime.handle_channel_webhook.assert_called_once_with("slack", envelope, auth)


class ProposeWorkflowIdempotencyTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        self.session = SessionRecord(
            session_id="slack:C123:thread-1",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"channel": "slack", "channel_id": "C123", "thread_id": "thread-1"},
        )
        get_session_store().save(self.session)

    def test_propose_start_workflow_is_idempotent_for_same_pending_type(self):
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            channel_ingress=True,
            session_id=self.session.session_id,
        )
        first = propose_start_workflow_handler(
            {
                "workflow_type": "prd_request",
                "summary": "PRD for checker game",
                "input": {"topic": "checkers"},
            },
            ctx,
        )
        self.assertEqual(first["status"], "awaiting_confirmation")

        second = propose_start_workflow_handler(
            {
                "workflow_type": "prd_request",
                "summary": "PRD for checker game",
                "input": {"topic": "checkers"},
            },
            ctx,
        )
        self.assertEqual(second["status"], "awaiting_confirmation")
        session = get_session_store().get(self.session.session_id)
        assert session is not None
        pending = (session.metadata or {}).get("pending_workflow") or {}
        self.assertEqual(pending.get("workflow_type"), "prd_request")


class WorkflowProposeFailureReplyTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()

    def test_propose_success_returns_formatted_confirmation_reply(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        config_path = repo_root / "config" / "ts-harness-runtime.test.yaml"
        runtime = RuntimeState(str(config_path))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        audit_events: list[str] = []
        runtime.audit.append = lambda **kwargs: audit_events.append(
            str(kwargs.get("action_type"))
        )
        request_text = "Create a PRD for an online multiplayer checker game"

        with mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
            result = runtime.handle_session_message(
                {
                    "tenant_id": "default",
                    "channel": "slack",
                    "channel_id": "C123",
                    "thread_id": "thread-1",
                    "text": request_text,
                },
                auth,
            )

        self.assertEqual(result["status"], "succeeded")
        summary = result["output"]["summary"]
        self.assertIn("I've prepared a prd request workflow for:", summary)
        self.assertIn("Reply yes to start, or no to cancel.", summary)
        self.assertNotEqual(summary, request_text)
        self.assertIn("workflow_proposed", audit_events)
        run_chat.assert_not_called()

    def test_approval_gate_reply_is_deterministic_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        config_path = repo_root / "config" / "ts-harness-runtime.test.yaml"
        runtime = RuntimeState(str(config_path))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        audit_events: list[str] = []
        runtime.audit.append = lambda **kwargs: audit_events.append(
            str(kwargs.get("action_type"))
        )
        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="thread-1",
        )
        session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": "thread-1",
        }
        get_session_store().save(session)
        with mock.patch(
            "ts_harness.workflows.engine.get_workflow_engine"
        ) as engine_cls:
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
            engine.handle_approve.return_value = {
                "id": "gate-1",
                "approval_status": "approved",
            }
            with mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
                result = runtime.handle_session_message(
                    {
                        "tenant_id": "default",
                        "channel": "slack",
                        "channel_id": "C123",
                        "thread_id": "thread-1",
                        "text": "approved",
                    },
                    auth,
                )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("work orders", result["output"]["summary"].lower())
        self.assertIn("jira", result["output"]["summary"].lower())
        self.assertIn("workflow_task_approved", audit_events)
        engine.handle_approve.assert_called_once()
        run_chat.assert_not_called()

    def test_thread_reply_decides_a_parked_tool_approval_and_resumes_the_stage(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState
        from ts_harness.tools import approvals as approvals_mod

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
        runtime.config.identity.roles = {"ada@example.com": ["approver"]}
        auth = AuthContext(tenant_id="default", actor_id="slack:U12345678")
        audit_events: list[str] = []
        runtime.audit.append = lambda **kwargs: audit_events.append(str(kwargs.get("action_type")))
        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="thread-tool",
        )
        session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "content_campaign",
            "mode": "workflow_linked",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": "thread-tool",
        }
        get_session_store().save(session)
        queue = approvals_mod.ApprovalQueue()
        req = queue.request(
            tenant_id="default",
            agent_id="marketing-agent",
            tool_name="google_workspace_docs_append",
            args={"document_id": "d1"},
            reason="approval required",
            workflow_id="wf-1",
            task_id="publish-1",
        )
        memory = mock.Mock()
        with mock.patch("ts_harness.workflows.engine.get_workflow_engine") as engine_cls, mock.patch.object(
            approvals_mod, "_approval_queue", queue
        ), mock.patch("ts_harness.memory.client.get_memory_client", return_value=memory), mock.patch.object(
            runtime.agent, "run_channel_chat"
        ) as run_chat:
            engine_cls.return_value.get_status.return_value = {
                "tasks": [
                    {
                        "id": "publish-1",
                        "step_name": "publish",
                        "status": "blocked",
                        "error": f"awaiting_tool_approval:{req.approval_id}",
                    }
                ]
            }
            result = runtime.handle_session_message(
                {
                    "tenant_id": "default",
                    "channel": "slack",
                    "channel_id": "C123",
                    "thread_id": "thread-tool",
                    "text": "approve",
                    "sender_email": "ada@example.com",
                },
                auth,
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            result["output"]["summary"],
            "Approved `google_workspace_docs_append`. Resuming step `publish`.",
        )
        self.assertEqual(queue.get(req.approval_id).status, "approved")
        memory.resume_tool_approval.assert_called_once_with(
            "publish-1", req.approval_id, actor="slack:U12345678"
        )
        self.assertIn("workflow_tool_approval_decided", audit_events)
        run_chat.assert_not_called()

    def test_approval_gate_looks_good_is_deterministic_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        config_path = repo_root / "config" / "ts-harness-runtime.test.yaml"
        runtime = RuntimeState(str(config_path))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="thread-1",
        )
        session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": "thread-1",
        }
        get_session_store().save(session)
        with mock.patch(
            "ts_harness.workflows.engine.get_workflow_engine"
        ) as engine_cls:
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
            engine.handle_approve.return_value = {
                "id": "gate-1",
                "approval_status": "approved",
            }
            with mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
                result = runtime.handle_session_message(
                    {
                        "tenant_id": "default",
                        "channel": "slack",
                        "channel_id": "C123",
                        "thread_id": "thread-1",
                        "text": "looks good",
                    },
                    auth,
                )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("work orders", result["output"]["summary"].lower())
        self.assertIn("jira", result["output"]["summary"].lower())
        engine.handle_approve.assert_called_once()
        run_chat.assert_not_called()

    def test_approval_gate_reject_with_feedback_is_deterministic_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="thread-1",
        )
        session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": "thread-1",
        }
        get_session_store().save(session)
        with mock.patch(
            "ts_harness.workflows.engine.get_workflow_engine"
        ) as engine_cls:
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
            with mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
                result = runtime.handle_session_message(
                    {
                        "tenant_id": "default",
                        "channel": "slack",
                        "channel_id": "C123",
                        "thread_id": "thread-1",
                        "text": "not yet, add audio chat",
                    },
                    auth,
                )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("sent your feedback", result["output"]["summary"].lower())
        engine.handle_reject.assert_called_once()
        self.assertEqual(
            engine.handle_reject.call_args.kwargs["comments"],
            "add audio chat",
        )
        run_chat.assert_not_called()

    def test_approval_gate_ambiguous_returns_clarification_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="thread-1",
        )
        session.metadata = {
            "workflow_id": "wf-1",
            "workflow_type": "prd_request",
            "mode": "workflow_linked",
            "channel": "slack",
            "channel_id": "C123",
            "thread_id": "thread-1",
        }
        get_session_store().save(session)
        with mock.patch(
            "ts_harness.workflows.engine.get_workflow_engine"
        ) as engine_cls:
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
            with mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
                result = runtime.handle_session_message(
                    {
                        "tenant_id": "default",
                        "channel": "slack",
                        "channel_id": "C123",
                        "thread_id": "thread-1",
                        "text": "maybe",
                    },
                    auth,
                )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("approve this PRD", result["output"]["summary"])
        engine.handle_approve.assert_not_called()
        engine.handle_reject.assert_not_called()
        run_chat.assert_not_called()

    def test_propose_failure_returns_deterministic_reply_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        audit_events: list[str] = []
        runtime.audit.append = lambda **kwargs: audit_events.append(str(kwargs.get("action_type")))

        with mock.patch(
            "ts_harness.gateway.channel_confirmations.execute_governed_workflow_proposal",
            return_value={"error": "session already linked to a workflow"},
        ):
            with mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
                result = runtime.handle_session_message(
                    {
                        "tenant_id": "default",
                        "channel": "slack",
                        "channel_id": "C123",
                        "thread_id": "thread-1",
                        "text": "Create a PRD for an online multiplayer checker game",
                    },
                    auth,
                )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("could not start the prd request workflow", result["output"]["summary"].lower())
        self.assertIn("workflow_propose_failed", audit_events)
        run_chat.assert_not_called()

    def test_approved_prd_work_orders_sets_bridge_pending_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        runtime.audit.append = lambda **kwargs: None

        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="thread-1",
        )
        session.metadata = {
            "mode": "workflow_linked",
            "workflow_id": "wf-prd",
            "source_workflow_id": "wf-prd",
            "workflow_type": "prd_request",
            "workflow_phase": "approved",
            "approved_artifact": {
                "result": {"prd_markdown": "# Approved PRD"}
            },
        }
        get_session_store().save(session)

        with mock.patch(
            "ts_harness.gateway.channel_confirmations."
            "should_auto_handle_intake_response",
            return_value=False,
        ), mock.patch(
            "ts_harness.gateway.channel_confirmations."
            "should_intercept_approval_gate_reply",
            return_value=False,
        ), mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
            result = runtime.handle_session_message(
                {
                    "tenant_id": "default",
                    "channel": "slack",
                    "channel_id": "C123",
                    "thread_id": "thread-1",
                    "text": "plan work orders",
                },
                auth,
            )

        saved = get_session_store().get(session.session_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("work orders", result["output"]["summary"].lower())
        self.assertIn("pending_bridge", saved.metadata or {})
        self.assertEqual(
            saved.metadata["pending_bridge"]["target_workflow_type"],
            "work_orders_from_prd",
        )
        run_chat.assert_not_called()

    def test_bridge_started_confirmation_returns_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.delegate_confirmation import (
            ContinueDeliveryConfirmation,
        )
        from ts_harness.gateway.delegate_confirmation import set_pending_bridge
        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        audit_events: list[str] = []
        runtime.audit.append = lambda **kwargs: audit_events.append(
            str(kwargs.get("action_type"))
        )

        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="C123",
            thread_id="thread-1",
        )
        set_pending_bridge(
            session,
            source_workflow_id="wf-prd",
            summary="plan work orders",
            target_workflow_type="work_orders_from_prd",
        )

        bridge_path = (
            "ts_harness.gateway.channel_confirmations."
            "execute_confirmed_bridge"
        )
        confirm_path = (
            "ts_harness.gateway.delegate_confirmation."
            "resolve_continue_delivery_confirmation"
        )
        with mock.patch(
            confirm_path,
            return_value=ContinueDeliveryConfirmation(confirmed=True),
        ), mock.patch(
            bridge_path,
            return_value={
                "status": "bridge_started",
                "target_workflow_id": "wf-wo",
                "target_workflow_type": "work_orders_from_prd",
                "first_step": "refine_requirements",
            },
        ), mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
            result = runtime.handle_session_message(
                {
                    "tenant_id": "default",
                    "channel": "slack",
                    "channel_id": "C123",
                    "thread_id": "thread-1",
                    "text": "yes",
                },
                auth,
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertIn("work orders from prd", result["output"]["summary"])
        self.assertIn("workflow_bridged", audit_events)
        run_chat.assert_not_called()

    def test_confirm_failure_returns_deterministic_reply_without_llm(self):
        from pathlib import Path

        from ts_harness.gateway.delegate_confirmation import set_pending_workflow
        from ts_harness.gateway.server import RuntimeState

        repo_root = Path(__file__).resolve().parents[2]
        os.environ["TS_HARNESS_SKIP_MCP_DISCOVERY"] = "true"
        os.environ["RUNTIME_FAKE_PROVIDER"] = "true"
        os.environ["SKILLS_DIR"] = str(repo_root / "skills")
        runtime = RuntimeState(str(repo_root / "config" / "ts-harness-runtime.test.yaml"))
        runtime.config.identity.roles = {"tester@test.local": ["requester"]}
        auth = AuthContext(tenant_id="default", actor_id="slack:U1")
        audit_events: list[str] = []
        runtime.audit.append = lambda **kwargs: audit_events.append(str(kwargs.get("action_type")))

        session = get_session_store().get_or_create(
            tenant_id="default",
            source="slack",
            agent_id="orchestrator",
            channel="slack",
            thread_id="thread-1",
        )
        set_pending_workflow(
            session,
            workflow_type="prd_request",
            summary="Create a PRD for an online multiplayer checkers game",
        )

        start_path = (
            "ts_harness.gateway.channel_confirmations."
            "execute_confirmed_workflow_start"
        )
        with mock.patch(
            start_path,
            return_value={"error": 'relation "delivery_programs" does not exist'},
        ):
            with mock.patch.object(runtime.agent, "run_channel_chat") as run_chat:
                result = runtime.handle_session_message(
                    {
                        "tenant_id": "default",
                        "channel": "slack",
                        "channel_id": "C123",
                        "thread_id": "thread-1",
                        "text": "yes",
                        "sender_email": "tester@test.local",
                    },
                    auth,
                )

        self.assertEqual(result["status"], "succeeded")
        summary = result["output"]["summary"].lower()
        self.assertIn("could not start the prd request workflow", summary)
        self.assertIn("delivery_programs", summary)
        self.assertIn("workflow_start_failed", audit_events)
        run_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
