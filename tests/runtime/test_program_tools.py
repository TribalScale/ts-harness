"""Tests for delivery program tool handlers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.workflow_tools import (
    advance_program_handler,
    is_program_managed_workflow,
    link_program_handler,
    propose_advance_program_handler,
    propose_bridge_workflow_handler,
    propose_start_program_handler,
    program_managed_workflow_types,
    program_status_handler,
    set_program_autoshare_handler,
    start_program_handler,
    unlink_program_handler,
)


class ProgramToolTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()

    def test_propose_start_program_sets_pending(self):
        session = SessionRecord(
            session_id="s-prog",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"channel": "slack", "channel_id": "C1", "thread_id": "t1"},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-prog")

        result = propose_start_program_handler(
            {
                "entry_phase": "jira",
                "summary": "File Jira from PRD",
                "artifacts": {"approved_prd": {"title": "PRD"}},
            },
            ctx,
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        saved = get_session_store().get("s-prog")
        self.assertEqual(saved.metadata["pending_program"]["entry_phase"], "jira")
        self.assertEqual(
            saved.metadata["pending_program"]["program_type"],
            "software_delivery",
        )

    def test_propose_start_program_reports_inferred_project(self):
        session = SessionRecord(
            session_id="s-infer",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"channel": "slack", "channel_id": "C1", "thread_id": "t1"},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            session_id="s-infer",
        )
        result = propose_start_program_handler(
            {
                "entry_phase": "jira",
                "summary": "File Jira from PRD",
                "artifacts": {
                    "approved_prd": {"title": "PRD"},
                    "jira_project_key": "TFS",
                },
            },
            ctx,
        )
        self.assertEqual(result["project_id"], "TFS")
        self.assertTrue(result["project_id_inferred"])
        self.assertIn("TFS", result["message"])
        self.assertIn("link_program_ids", result["message"])

    def test_propose_start_program_accepts_explicit_program_type(self):
        session = SessionRecord(
            session_id="s-prog-type",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"channel": "slack", "channel_id": "C1", "thread_id": "t1"},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            session_id="s-prog-type",
        )
        result = propose_start_program_handler(
            {
                "entry_phase": "prd",
                "summary": "Start custom program",
                "program_type": "software_delivery",
            },
            ctx,
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result.get("program_type"), "software_delivery")
        saved = get_session_store().get("s-prog-type")
        self.assertEqual(
            saved.metadata["pending_program"]["program_type"],
            "software_delivery",
        )

    def test_program_managed_workflow_types_come_from_registry(self):
        managed = program_managed_workflow_types()
        self.assertIn("jira_from_prd", managed)
        self.assertIn("implement_work_orders", managed)
        self.assertIn("deploy_release", managed)
        self.assertIn("rollback_release", managed)
        self.assertTrue(is_program_managed_workflow("work_orders_from_prd"))
        self.assertTrue(is_program_managed_workflow("governed_delivery"))
        self.assertFalse(is_program_managed_workflow("prd_request"))

    def test_deploy_release_remains_in_workflow_catalog(self):
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton("deploy_release")
        self.assertEqual(skeleton.workflow_type, "deploy_release")
        self.assertEqual(skeleton.program.get("phase"), "release")

    def test_propose_bridge_stores_program_identity_nodes(self):
        session = SessionRecord(
            session_id="s-bridge",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "channel": "slack",
                "channel_id": "C1",
                "thread_id": "t1",
                "workflow_id": "wf-prd",
                "source_workflow_id": "wf-prd",
                "workflow_type": "prd_request",
                "workflow_phase": "approved",
                "mode": "workflow_linked",
                "program_id": "prog-1",
                "program_phase": "prd",
                "program_type": "software_delivery",
            },
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            session_id="s-bridge",
        )
        result = propose_bridge_workflow_handler(
            {
                "summary": "create jira tickets",
                "target_workflow_type": "jira_from_prd",
            },
            ctx,
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        pending = get_session_store().get("s-bridge").metadata["pending_bridge"]
        self.assertEqual(pending["program_type"], "software_delivery")
        self.assertEqual(pending["source_node"], "prd")
        self.assertEqual(pending["target_node"], "jira")
        self.assertEqual(pending["target_workflow_type"], "jira_from_prd")

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_advance_program_rejects_stale_pending_target(self, service_factory):
        service = MagicMock()
        service_factory.return_value = service
        session = SessionRecord(
            session_id="s-stale",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "channel": "slack",
                "channel_id": "C1",
                "thread_id": "t1",
                "program_id": "prog-1",
                "program_phase": "jira",
                "program_type": "software_delivery",
                "workflow_id": "wf-jira",
                "workflow_type": "jira_from_prd",
                "pending_program_advance": {
                    "program_id": "prog-1",
                    "program_type": "software_delivery",
                    "source_node": "prd",
                    "target_node": "jira",
                    "summary": "stale confirmation",
                },
            },
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            session_id="s-stale",
        )
        result = advance_program_handler({}, ctx)
        self.assertIn("error", result)
        self.assertIn("stale", result["error"].lower())
        service.advance_program.assert_not_called()

    def test_propose_advance_program_stores_source_and_target_nodes(self):
        session = SessionRecord(
            session_id="s-adv",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "channel": "slack",
                "channel_id": "C1",
                "thread_id": "t1",
                "program_id": "prog-1",
                "program_phase": "prd",
                "program_type": "software_delivery",
                "workflow_id": "wf-prd",
                "workflow_type": "prd_request",
            },
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            session_id="s-adv",
        )
        result = propose_advance_program_handler(
            {"summary": "Continue to Jira", "target_phase": "jira"},
            ctx,
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        pending = get_session_store().get("s-adv").metadata["pending_program_advance"]
        self.assertEqual(pending["program_type"], "software_delivery")
        self.assertEqual(pending["source_node"], "prd")
        self.assertEqual(pending["target_node"], "jira")

    def test_propose_start_program_rejects_missing_artifacts(self):
        session = SessionRecord(
            session_id="s-prog2",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-prog2")

        result = propose_start_program_handler(
            {"entry_phase": "jira", "summary": "File Jira"},
            ctx,
        )
        self.assertIn("error", result)

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_start_program_creates_program_session(self, service_factory):
        service = MagicMock()
        service.create_program.return_value = MagicMock(
            program_id="prog-1",
            workflow_id="wf-1",
            workflow_type="jira_from_prd",
            entry_phase="jira",
            task_ids=["t1"],
            first_step="create_jira",
        )
        service_factory.return_value = service

        session = SessionRecord(
            session_id="s-start",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "channel": "slack",
                "channel_id": "C1",
                "thread_id": "t1",
                "pending_program": {
                    "entry_phase": "jira",
                    "summary": "File Jira",
                    "artifacts": {"approved_prd": {"title": "PRD"}},
                    "program_type": "software_delivery",
                    "project_id": "TFS",
                    "project_id_source": "user",
                    "link_program_ids": ["prog-b"],
                },
            },
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-start")

        result = start_program_handler({}, ctx)
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["program_id"], "prog-1")
        saved = get_session_store().get("s-start")
        self.assertEqual(saved.metadata.get("program_id"), "prog-1")
        create_kwargs = service.create_program.call_args.kwargs
        self.assertEqual(create_kwargs.get("program_type"), "software_delivery")
        self.assertEqual(create_kwargs.get("project_id"), "TFS")
        self.assertEqual(create_kwargs.get("link_program_ids"), ["prog-b"])

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_program_status_returns_aggregate(self, service_factory):
        service = MagicMock()
        service.get_status.return_value = {"program_id": "prog-1", "status": "running"}
        service_factory.return_value = service

        session = SessionRecord(
            session_id="s-status",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"program_id": "prog-1"},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-status")

        result = program_status_handler({}, ctx)
        self.assertEqual(result["program_id"], "prog-1")

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    @patch("ts_harness.tools.workflow_tools.get_memory_client")
    def test_program_status_resolves_a_unique_display_name(self, client_factory, service_factory):
        from ts_harness.memory.client import ProgramNotFound

        client = MagicMock()
        client.get_program.side_effect = ProgramNotFound("missing")
        client.list_programs.return_value = {
            "programs": [
                {"id": "prog-9", "display_name": "Checkout flow", "summary": "tickets"},
            ]
        }
        client_factory.return_value = client
        service = MagicMock()
        service.get_status.return_value = {"program_id": "prog-9", "status": "cancelled"}
        service_factory.return_value = service
        session = SessionRecord(
            session_id="s-name",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-name"
        )

        result = program_status_handler({"program_id": "Checkout flow"}, ctx)

        service.get_status.assert_called_once_with("prog-9")
        self.assertEqual(result["status"], "cancelled")

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    @patch("ts_harness.tools.workflow_tools.get_memory_client")
    def test_program_status_rejects_an_ambiguous_display_name(self, client_factory, service_factory):
        from ts_harness.memory.client import ProgramNotFound

        client = MagicMock()
        client.get_program.side_effect = ProgramNotFound("missing")
        client.list_programs.return_value = {
            "programs": [
                {"id": "prog-1", "display_name": "Checkout flow", "summary": "one"},
                {"id": "prog-2", "display_name": "Checkout flow", "summary": "two"},
            ]
        }
        client_factory.return_value = client
        service = MagicMock()
        service_factory.return_value = service
        session = SessionRecord(
            session_id="s-ambiguous",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            session_id="s-ambiguous",
        )

        result = program_status_handler({"program_id": "Checkout flow"}, ctx)

        self.assertIn("ambiguous", result["error"])
        service.get_status.assert_not_called()

    @patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    @patch("ts_harness.tools.workflow_tools.get_memory_client")
    def test_program_status_uses_a_found_id_without_listing(self, client_factory, service_factory):
        program_id = "11111111-1111-1111-1111-111111111111"
        client = MagicMock()
        client.get_program.return_value = {"program": {"id": program_id}}
        client_factory.return_value = client
        service = MagicMock()
        service.get_status.return_value = {"program_id": program_id, "status": "running"}
        service_factory.return_value = service
        session = SessionRecord(
            session_id="s-id",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(
            tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-id"
        )

        program_status_handler({"program_id": program_id}, ctx)

        service.get_status.assert_called_once_with(program_id)
        client.list_programs.assert_not_called()

    @patch("ts_harness.tools.workflow_tools.get_memory_client")
    def test_link_rejected_across_tenants(self, client_factory):
        client = MagicMock()
        client.create_program_link.side_effect = ValueError("cross-tenant program link rejected")
        client_factory.return_value = client
        session = SessionRecord(
            session_id="s-link",
            routing_key="rk",
            tenant_id="tenant-a",
            agent_id="orchestrator",
            source="slack",
            metadata={"program_id": "prog-a"},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(tenant_id="tenant-a", agent_id="orchestrator", source="slack", session_id="s-link")
        result = link_program_handler({"to_program_id": "prog-b"}, ctx)
        self.assertIn("cross-tenant", result["error"])

    @patch("ts_harness.tools.workflow_tools.get_memory_client")
    def test_unlink_removes_only_that_edge(self, client_factory):
        client = MagicMock()
        client.delete_program_link.return_value = {
            "from_program_id": "prog-a",
            "to_program_id": "prog-b",
            "deleted": True,
        }
        client_factory.return_value = client
        session = SessionRecord(
            session_id="s-unlink",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"program_id": "prog-a"},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-unlink")
        result = unlink_program_handler({"to_program_id": "prog-b"}, ctx)
        self.assertTrue(result["deleted"])
        client.delete_program_link.assert_called_once_with(
            "prog-a",
            "prog-b",
            tenant_id="default",
            actor="orchestrator",
        )
        client.patch_program_artifacts.assert_called()

    @patch("ts_harness.tools.workflow_tools.get_memory_client")
    def test_exclude_hides_from_autoshare(self, client_factory):
        client = MagicMock()
        client.patch_program.return_value = {"program": {"id": "prog-a"}}
        client.get_program.return_value = {"program": {"id": "prog-a", "project_id": "TFS"}}
        client_factory.return_value = client
        session = SessionRecord(
            session_id="s-ex",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"program_id": "prog-a"},
        )
        get_session_store().save(session)
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="slack", session_id="s-ex")
        result = set_program_autoshare_handler({"excluded": True}, ctx)
        self.assertTrue(result["excluded_from_autoshare"])
        client.patch_program.assert_called_once()
        self.assertTrue(client.patch_program.call_args.kwargs["excluded_from_autoshare"])


if __name__ == "__main__":
    unittest.main()
