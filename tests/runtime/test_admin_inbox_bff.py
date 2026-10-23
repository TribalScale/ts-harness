"""Tests for admin inbox aggregate and auth config."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ts_harness.agent.sessions import SessionRecord
from ts_harness.gateway.admin_bff import (
    auth_config_payload,
    build_inbox,
    is_admin,
    session_messages_admin,
)
from ts_harness.gateway.auth import AuthContext


class AdminBffTests(unittest.TestCase):
    def test_auth_config_static(self):
        runtime = SimpleNamespace(config=SimpleNamespace(identity=SimpleNamespace(provider="static")))
        payload = auth_config_payload(runtime)
        self.assertEqual(payload["mode"], "static")

    def test_auth_config_oidc(self):
        runtime = SimpleNamespace(
            config=SimpleNamespace(
                identity=SimpleNamespace(
                    provider="oidc",
                    issuer="https://idp.example",
                    audience="ec",
                    jwks_url="https://idp.example/jwks",
                )
            )
        )
        payload = auth_config_payload(runtime)
        self.assertEqual(payload["mode"], "oidc")
        self.assertEqual(payload["authorization_endpoint"], "https://idp.example/authorize")

    def test_is_admin_without_roles(self):
        auth = AuthContext(actor_id="ops", tenant_id="default", roles=None)
        runtime = SimpleNamespace(policy=SimpleNamespace())
        self.assertTrue(is_admin(auth, runtime))

    def test_build_inbox_merges_sources(self):
        approval = SimpleNamespace(
            approval_id="appr-1",
            tool_name="gmail.send",
            agent_id="pm-agent",
            status="pending",
            requested_at="2026-08-01T00:00:00Z",
        )
        queue = SimpleNamespace(_requests={"appr-1": approval})
        memory = mock.Mock()
        memory.list_programs.return_value = {
            "programs": [
                {
                    "id": "prog-1",
                    "status": "blocked",
                    "display_name": "Delivery",
                    "summary": "Delivery",
                }
            ]
        }
        memory.get_program_status.return_value = {
            "waiting_on": {
                "kind": "approval",
                "since": "2026-08-01T01:00:00Z",
                "match": {"task_id": "task-1", "step_name": "approve_prd"},
            },
            "recommended_action": "approve_or_reject_pending_gate",
        }
        runtime = SimpleNamespace(
            policy=SimpleNamespace(
                evaluate_identity=mock.Mock(return_value=SimpleNamespace(decision="allow"))
            )
        )
        auth = AuthContext(actor_id="admin", tenant_id="default", roles=["admin"])
        with mock.patch(
            "ts_harness.skills.authoring.list_drafts",
            return_value=[{"id": "draft/a", "approval_status": "draft"}],
        ), mock.patch(
            "ts_harness.gateway.admin_bff.get_approval_queue", return_value=queue
        ):
            result = build_inbox(runtime, auth, memory=memory, skills_root="skills")
        types = {item["type"] for item in result["items"]}
        self.assertIn("skill_draft", types)
        self.assertIn("tool_approval", types)
        self.assertIn("workflow_gate", types)

    def test_build_inbox_lists_steps_waiting_for_a_human_decision(self):
        from ts_harness.workflows.engine import WorkflowEngine
        from ts_harness.workflows.hooks import build_default_registry

        stuck = {
            "id": "finalize-4",
            "workflow_id": "wf-d",
            "workflow_type": "deliver_work_order",
            "step_name": "finalize_code_review",
            "error": "barrier_blocked: code_review_blocked: 1 blocking findings",
            "input": {"_awaiting_human_decision": {"reason": "revision_limit"}},
            "output": {"review_feedback": "Fix the persistence bug."},
            "updated_at": "2026-10-22T17:34:10Z",
            "parent_workflow_id": "wf-impl",
        }
        deliver = {
            "id": "deliver-1",
            "step_name": "deliver_work_order",
            "status": "succeeded",
            "input": {"work_order_id": "WO-003"},
            "output": {"pr_url": "https://github.com/org/app/pull/18"},
        }
        memory = mock.Mock()
        memory.list_programs.return_value = {"programs": []}
        memory.list_stuck_tasks.return_value = [stuck]
        payloads = {
            "wf-d": {"workflow": {"id": "wf-d", "workflow_type": "deliver_work_order", "parent_workflow_id": "wf-impl"}, "tasks": [deliver, {**stuck, "status": "blocked"}]},
            "wf-impl": {"workflow": {"id": "wf-impl", "program_id": "prog-1"}, "tasks": []},
        }
        memory.get_workflow.side_effect = lambda workflow_id: payloads[workflow_id]
        engine = WorkflowEngine(client=memory, registry=build_default_registry())
        auth = AuthContext(actor_id="ops", tenant_id="acme", roles=None)
        runtime = SimpleNamespace(policy=SimpleNamespace())
        with (
            mock.patch("ts_harness.gateway.admin_bff.get_approval_queue", return_value=SimpleNamespace(_requests={})),
            mock.patch("ts_harness.workflows.engine.get_workflow_engine", return_value=engine),
            mock.patch("ts_harness.skills.authoring.list_drafts", return_value=[]),
        ):
            result = build_inbox(runtime, auth, memory=memory, skills_root="skills")

        memory.list_stuck_tasks.assert_called_once_with(tenant_id="acme")
        [item] = [item for item in result["items"] if item["type"] == "stuck_step"]
        self.assertEqual(item["id"], "finalize-4")
        self.assertEqual(item["title"], "WO-003 · finalize_code_review")
        meta = item["meta"]
        self.assertEqual(meta["decisions"], ["revise", "accept", "stop"])
        self.assertEqual((meta["workflow_id"], meta["program_id"]), ("wf-d", "prog-1"))
        self.assertEqual(meta["pr_url"], "https://github.com/org/app/pull/18")
        self.assertIn("Fix the persistence bug.", meta["feedback"])
        self.assertEqual(meta["status"], "blocked")


class SessionMessagesTenantScopeTests(unittest.TestCase):
    def setUp(self):
        self.session = SessionRecord(
            session_id="session-1",
            routing_key="tenant-a:web",
            tenant_id="tenant-a",
            agent_id="ops-agent",
            source="web",
        )
        store = mock.Mock()
        store.get.return_value = self.session
        transcript = mock.Mock()
        transcript.load_messages.return_value = [{"role": "user", "content": "hello"}]
        patches = [
            mock.patch(
                "ts_harness.agent.sessions.get_session_store",
                return_value=store,
            ),
            mock.patch(
                "ts_harness.agent.transcript.get_transcript_store",
                return_value=transcript,
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = store

    def test_same_tenant_reads_transcript(self):
        auth = AuthContext(actor_id="admin", tenant_id="tenant-a", roles=["admin"])

        result = session_messages_admin("session-1", auth)

        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["agent_id"], "ops-agent")
        self.assertEqual(result["messages"], [{"role": "user", "content": "hello"}])
        self.assertNotIn("program_id", result)

    def test_program_id_comes_from_session_metadata(self):
        self.session.metadata = {"program_id": "program-1"}
        auth = AuthContext(actor_id="admin", tenant_id="tenant-a", roles=["admin"])

        result = session_messages_admin("session-1", auth)

        self.assertEqual(result["program_id"], "program-1")

    def test_cross_tenant_read_is_not_found(self):
        auth = AuthContext(actor_id="admin", tenant_id="tenant-b", roles=["admin"])

        with self.assertRaises(KeyError):
            session_messages_admin("session-1", auth)

    def test_unknown_session_is_not_found(self):
        self.store.get.return_value = None
        auth = AuthContext(actor_id="admin", tenant_id="tenant-a", roles=["admin"])

        with self.assertRaises(KeyError):
            session_messages_admin("missing", auth)


if __name__ == "__main__":
    unittest.main()
