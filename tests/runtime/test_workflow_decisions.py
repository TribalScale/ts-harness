"""Recorded decisions: gate roles and expiry, track choice, governance setting."""

from __future__ import annotations

import importlib.util
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "scripts" / "memory-gateway.py"


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_decisions", GATEWAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoutedCursor:
    def __init__(self, routes: list[tuple[str, Any]]):
        self.routes = routes
        self.calls: list[tuple[str, Any]] = []
        self._last: Any = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = None
        for fragment, result in self.routes:
            if fragment in sql:
                self._last = result
                break

    def fetchone(self):
        if isinstance(self._last, list):
            return self._last[0] if self._last else None
        return self._last

    def fetchall(self):
        if isinstance(self._last, list):
            return self._last
        return [self._last] if self._last else []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def decisions(self) -> list[tuple]:
        return [params for sql, params in self.calls if "INSERT INTO workflow_decisions" in sql]

    def audit_actions(self) -> list[str]:
        return [params[2] for sql, params in self.calls if "INSERT INTO session_audit_log" in sql]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def _gate(policy: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "status": "pending",
        "approval_required": True,
        "step_name": "approve_prd",
        "input": {"_approval_policy": policy} if policy else {},
        "workflow_id": str(uuid.uuid4()),
        "tenant_id": "acme",
        "depends_on": [],
        "expires_at": None,
        "created_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


class GateDecisionTests(unittest.TestCase):
    def setUp(self):
        self.gateway = load_gateway()
        self.task_id = str(uuid.uuid4())

    def _approve(self, task: dict[str, Any], body: dict[str, Any], approved: bool = True):
        cursor = RoutedCursor([("FROM workflow_tasks\n            WHERE id", task)])
        conn = FakeConnection(cursor)
        payload = {"id": self.task_id, "actor": "slack:U1", **body}
        return self.gateway.approve_workflow_task(conn, payload, approved), cursor, conn

    def test_approve_writes_gate_decision_row(self):
        result, cursor, _ = self._approve(
            _gate(),
            {"actor_email": "Pat@Acme.com", "actor_roles": ["pm"], "program_id": str(uuid.uuid4())},
        )
        self.assertIn("decision_id", result)
        decision = cursor.decisions()[0]
        self.assertEqual(decision[5:8], ("gate", "approved", "slack:U1"))
        self.assertEqual(decision[8], "pat@acme.com")
        self.assertIn("workflow_decision_recorded", cursor.audit_actions())

    def test_reject_writes_rejected_decision_with_reason(self):
        _, cursor, _ = self._approve(_gate(), {"reason": "scope unclear"}, approved=False)
        decision = cursor.decisions()[0]
        self.assertEqual(decision[6], "rejected")
        self.assertEqual(decision[10], "scope unclear")

    def test_gate_of_another_tenant_is_not_found(self):
        with self.assertRaisesRegex(ValueError, "workflow task not found"):
            _, cursor, _ = self._approve(_gate(), {"tenant_id": "other"})
        _, cursor, _ = self._approve(_gate(), {"tenant_id": "acme"})
        self.assertTrue([sql for sql, _ in cursor.calls if "SET approval_status" in sql])

    def test_actor_without_required_role_is_denied(self):
        with self.assertRaisesRegex(ValueError, "requires one of roles: approver"):
            self._approve(_gate({"required_roles": ["approver"]}), {"actor_roles": ["member"]})

    def test_denied_attempt_is_recorded(self):
        cursor = RoutedCursor(
            [("FROM workflow_tasks\n            WHERE id", _gate({"required_roles": ["approver"]}))]
        )
        conn = FakeConnection(cursor)
        with self.assertRaises(ValueError):
            self.gateway.approve_workflow_task(
                conn, {"id": self.task_id, "actor": "slack:U2", "actor_roles": []}, True
            )
        self.assertEqual(cursor.decisions()[0][6], "denied")
        self.assertIn("workflow_gate_denied", cursor.audit_actions())
        self.assertEqual(conn.commits, 1)
        self.assertFalse([sql for sql, _ in cursor.calls if "SET approval_status" in sql])

    def test_actor_with_required_role_passes(self):
        result, _, _ = self._approve(
            _gate({"required_roles": ["approver"]}), {"actor_roles": ["Approver"]}
        )
        self.assertEqual(result["status"], "succeeded")

    def test_viewer_cannot_cover_the_approver_bundle(self):
        with self.assertRaisesRegex(ValueError, "requires one of roles: approver"):
            self._approve(_gate({"required_roles": ["approver"]}), {"actor_roles": ["viewer"]})

    def test_custom_permission_set_that_covers_approver_can_approve(self):
        result, _, _ = self._approve(
            _gate({"required_roles": ["approver"]}),
            {
                "actor_roles": ["release_captain"],
                "actor_permissions": ["view", "approve_gate", "confirm_delegation", "edit_config"],
            },
        )
        self.assertEqual(result["status"], "succeeded")

    def test_extra_required_permission_is_added_to_the_gate(self):
        with self.assertRaisesRegex(ValueError, "approval requires permission: approve_promote_production"):
            self._approve(
                _gate({"required_permissions": ["approve_gate"], "required_permission": "approve_promote_production"}),
                {"actor_roles": ["approver"]},
            )
        result, _, _ = self._approve(
            _gate({"required_permissions": ["approve_gate"], "required_permission": "approve_promote_production"}),
            {"actor_roles": ["approver"], "actor_permissions": ["approve_gate", "approve_promote_production"]},
        )
        self.assertEqual(result["status"], "succeeded")

    def test_required_permission_accepts_an_intersection(self):
        result, _, _ = self._approve(
            _gate({"required_permissions": ["approve_gate"]}),
            {"actor_roles": ["approver"]},
        )
        self.assertEqual(result["status"], "succeeded")

    def test_expired_gate_is_refused_and_escalated(self):
        task = _gate(
            {"expires_after_seconds": 60, "escalate_to_role": "eng_manager"},
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        cursor = RoutedCursor([("FROM workflow_tasks\n            WHERE id", task)])
        conn = FakeConnection(cursor)
        with self.assertRaisesRegex(ValueError, "expired.*escalated to eng_manager"):
            self.gateway.approve_workflow_task(conn, {"id": self.task_id, "actor": "u"}, True)
        self.assertEqual(cursor.decisions()[0][6], "expired")
        self.assertIn("workflow_gate_expired", cursor.audit_actions())

    def test_expiry_counts_from_dependency_completion(self):
        dep = str(uuid.uuid4())
        task = _gate(
            {"expires_after_seconds": 3600},
            depends_on=[dep],
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        cursor = RoutedCursor(
            [
                ("FROM workflow_tasks\n            WHERE id", task),
                ("MAX(completed_at)", {"ready_at": datetime.now(timezone.utc) - timedelta(minutes=5)}),
            ]
        )
        result = self.gateway.approve_workflow_task(
            FakeConnection(cursor), {"id": self.task_id, "actor": "u"}, True
        )
        self.assertEqual(result["status"], "succeeded")

    def test_reject_is_allowed_after_expiry(self):
        task = _gate(
            {"expires_after_seconds": 60},
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        result, _, _ = self._approve(task, {}, approved=False)
        self.assertEqual(result["approval_status"], "rejected")


TRACK_SNAPSHOT = {
    "program_type": "custom",
    "definition_version": "1.0.0",
    "startable_nodes": ["intake"],
    "nodes": [
        {"id": "intake", "default_workflow_type": "prd_request", "terminal": False},
        {"id": "plan_a", "default_workflow_type": "jira_from_prd", "terminal": False},
        {"id": "plan_b", "default_workflow_type": "work_orders_from_prd", "terminal": True},
    ],
    "edges": [
        {"source": "intake", "target": "plan_a", "mode": "manual", "signals": ["plan it"]},
        {"source": "intake", "target": "plan_b", "mode": "manual", "signals": ["plan it", "work orders"]},
    ],
}


class TrackChoiceTests(unittest.TestCase):
    def test_single_match_resolves(self):
        from ts_harness.workflows.program_materialize import (
            resolve_bridge_target_nodes_from_text,
        )

        node, workflow = resolve_bridge_target_nodes_from_text(
            "generate work orders", snapshot=TRACK_SNAPSHOT, source_node="intake"
        )
        self.assertEqual((node, workflow), ("plan_b", "work_orders_from_prd"))

    def test_multiple_matches_never_pick(self):
        from ts_harness.workflows.program_materialize import (
            resolve_bridge_candidates_from_text,
            resolve_bridge_target_nodes_from_text,
        )

        self.assertEqual(
            resolve_bridge_target_nodes_from_text("plan it", snapshot=TRACK_SNAPSHOT, source_node="intake"),
            (None, None),
        )
        candidates = resolve_bridge_candidates_from_text(
            "plan it", snapshot=TRACK_SNAPSHOT, source_node="intake"
        )
        self.assertEqual([c["target_node"] for c in candidates], ["plan_a", "plan_b"])

    def test_ambiguity_result_lists_candidates(self):
        from ts_harness.tools.workflow_tools import AmbiguousTrackChoice

        exc = AmbiguousTrackChoice(
            [
                {"target_node": "plan_a", "workflow_type": "jira_from_prd"},
                {"target_node": "plan_b", "workflow_type": "work_orders_from_prd"},
            ]
        )
        result = exc.as_result()
        self.assertEqual(result["status"], "choice_required")
        self.assertEqual(len(result["candidates"]), 2)

    def test_gateway_advance_records_track_choice(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "tenant_id": "acme",
            "current_phase": "intake",
            "program_definition_snapshot": TRACK_SNAPSHOT,
        }
        cursor = RoutedCursor([("FROM delivery_programs WHERE id", program)])
        result = gateway.advance_delivery_program(
            FakeConnection(cursor),
            program_id,
            {"target_phase": "plan_b", "actor": "slack:U1", "actor_email": "pat@acme.com"},
        )
        self.assertIn("decision_id", result)
        decision = cursor.decisions()[0]
        self.assertEqual(decision[5:7], ("track_choice", "plan_b"))

    def test_gateway_advance_without_target_is_rejected_when_ambiguous(self):
        gateway = load_gateway()
        program_id = str(uuid.uuid4())
        program = {
            "id": program_id,
            "tenant_id": "acme",
            "current_phase": "intake",
            "program_definition_snapshot": TRACK_SNAPSHOT,
        }
        cursor = RoutedCursor([("FROM delivery_programs WHERE id", program)])
        with self.assertRaisesRegex(ValueError, "explicit target_phase required"):
            gateway.advance_delivery_program(FakeConnection(cursor), program_id, {})


class StagePolicyTests(unittest.TestCase):
    def _stage(self, **extra: Any) -> dict[str, Any]:
        stage = {"name": "approve", "kind": "task", "agent_id": "human", "approval_required": True}
        stage.update(extra)
        return {"workflow_type": "t", "definition_version": "1.0.0", "stages": [stage]}

    def test_policy_parses_and_normalizes(self):
        import workflow_definitions

        skeleton = workflow_definitions.load_skeleton_payload(
            self._stage(
                approval={
                    "required_roles": ["Approver", "approver"],
                    "expires_after_seconds": 3600,
                    "escalate_to_role": "Eng_Manager",
                }
            )
        )
        self.assertEqual(
            skeleton.stages[0].approval,
            {"required_roles": ["approver"], "expires_after_seconds": 3600, "escalate_to_role": "eng_manager"},
        )

    def test_policy_requires_approval_gate(self):
        import workflow_definitions

        with self.assertRaisesRegex(ValueError, "requires approval_required"):
            workflow_definitions.load_skeleton_payload(
                self._stage(approval_required=False, approval={"required_roles": ["x"]})
            )

    def test_policy_rejects_unknown_keys_and_bad_expiry(self):
        import workflow_definitions

        with self.assertRaisesRegex(ValueError, "unknown field"):
            workflow_definitions.load_skeleton_payload(self._stage(approval={"quorum": 2}))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            workflow_definitions.load_skeleton_payload(self._stage(approval={"expires_after_seconds": 0}))

    def test_materialize_seeds_policy_into_gate_input(self):
        import workflow_definitions

        from ts_harness.workflows.materialize import materialize_skeleton_tasks

        skeleton = workflow_definitions.load_skeleton_payload(
            self._stage(approval={"required_roles": ["approver"]})
        )
        bodies = materialize_skeleton_tasks(skeleton, workflow_id=str(uuid.uuid4()))
        self.assertEqual(bodies[0]["input"]["_approval_policy"], {"required_roles": ["approver"]})


class DecisionActorTests(unittest.TestCase):
    def test_slack_user_resolves_email_and_roles(self):
        from ts_harness.workflows.decisions import resolve_decision_actor

        config = SimpleNamespace(
            identity=SimpleNamespace(
                slack_user_emails={"U1": "pat@acme.com"},
                roles={"pat@acme.com": ["approver", "pm"]},
            )
        )
        with mock.patch(
            "ts_harness.gateway.channels.slack_users.resolve_slack_user_email",
            return_value="pat@acme.com",
        ):
            actor = resolve_decision_actor(
                {"slack_user_id": "U1"}, fallback_actor="orchestrator", config=config
            )
        self.assertEqual(actor.actor_id, "slack:U1")
        self.assertEqual(actor.email, "pat@acme.com")
        self.assertEqual(actor.roles, ("approver", "pm"))
        self.assertEqual(actor.as_fields()["actor_roles"], ["approver", "pm"])

    def test_decision_fields_carry_program(self):
        from ts_harness.workflows.decisions import decision_request_fields

        fields = decision_request_fields(
            {"actor_email": "pat@acme.com", "actor_roles": [], "ignored": 1},
            {"program_id": "p1"},
        )
        self.assertEqual(fields, {"actor_email": "pat@acme.com", "program_id": "p1"})


class GovernanceSettingTests(unittest.TestCase):
    def test_default_is_on(self):
        from ts_harness.config.governance import governance_snapshot, program_governance_snapshot

        self.assertTrue(governance_snapshot({})["independent_review"]["enabled"])
        self.assertTrue(program_governance_snapshot({"metadata": {}})["independent_review"]["enabled"])

    def test_pinned_snapshot_survives_settings_change(self):
        from ts_harness.config.governance import program_governance_snapshot

        program = {"metadata": {"governance_snapshot": {"independent_review": {"enabled": False}}}}
        self.assertFalse(program_governance_snapshot(program)["independent_review"]["enabled"])

    def test_settings_validation(self):
        from ts_harness.config.runtime_settings import validate_runtime_settings_payload

        canonical, issues = validate_runtime_settings_payload(
            {
                "definition_type": "runtime_settings",
                "definition_version": "1.0.0",
                "program_defaults": {"governance": {"independent_review": {"enabled": False}}},
            }
        )
        self.assertEqual(canonical["program_defaults"]["governance"], {"independent_review": {"enabled": False}})
        _, issues = validate_runtime_settings_payload(
            {
                "definition_type": "runtime_settings",
                "definition_version": "1.0.0",
                "program_defaults": {"governance": {"independent_review": {"enabled": "no"}}},
            }
        )
        self.assertIn("invalid_boolean", {issue["code"] for issue in issues})

    def _runtime(self, enabled: bool = True):
        config = SimpleNamespace(
            program_defaults={"governance": {"independent_review": {"enabled": enabled}}},
            identity=SimpleNamespace(slack_user_emails={}, jira_account_ids={}, github_logins={}, roles={}),
        )
        runtime = SimpleNamespace(config=config, policy=mock.Mock())
        runtime._yaml_settings_baseline = {"program_defaults": {}, "identity": {}}
        return runtime

    def _canonical(self, enabled: bool) -> dict[str, Any]:
        return {
            "definition_type": "runtime_settings",
            "definition_version": "1.0.0",
            "program_defaults": {"governance": {"independent_review": {"enabled": enabled}}},
        }

    def _auth(self, roles=None):
        from ts_harness.gateway.auth import AuthContext

        return AuthContext(actor_id="admin-1", tenant_id="acme", email="Admin@Acme.com", roles=roles)

    def test_unchanged_value_needs_no_reason(self):
        from ts_harness.gateway.runtime_settings_bff import guard_governance_change

        client = mock.Mock()
        result = guard_governance_change(
            self._runtime(True), self._auth(), {}, self._canonical(True), memory_client=client
        )
        self.assertIsNone(result)
        client.record_decision.assert_not_called()

    def test_non_admin_cannot_turn_off(self):
        from ts_harness.gateway.runtime_settings_bff import guard_governance_change

        runtime = self._runtime(True)
        runtime.policy.evaluate_identity.return_value = SimpleNamespace(decision="deny")
        with self.assertRaises(PermissionError):
            guard_governance_change(
                runtime,
                self._auth(["member"]),
                {"governance_reason": "x"},
                self._canonical(False),
                memory_client=mock.Mock(),
            )

    def test_reason_is_required(self):
        from ts_harness.gateway.runtime_settings_bff import guard_governance_change

        with self.assertRaisesRegex(ValueError, "governance_reason is required"):
            guard_governance_change(
                self._runtime(True), self._auth(), {}, self._canonical(False), memory_client=mock.Mock()
            )

    def test_admin_change_is_recorded_as_decision(self):
        from ts_harness.gateway.runtime_settings_bff import guard_governance_change

        client = mock.Mock()
        client.record_decision.return_value = {"id": "d1"}
        result = guard_governance_change(
            self._runtime(True),
            self._auth(),
            {"governance_reason": "solo maintainer repo"},
            self._canonical(False),
            memory_client=client,
        )
        self.assertFalse(result["after"]["independent_review"]["enabled"])
        kwargs = client.record_decision.call_args.kwargs
        self.assertEqual(kwargs["kind"], "governance_setting")
        self.assertEqual(kwargs["decision"], "independent_review.enabled=false")
        self.assertEqual(kwargs["reason"], "solo maintainer repo")
        self.assertEqual(kwargs["actor_email"], "admin@acme.com")

    def test_failed_audit_aborts_change(self):
        from ts_harness.gateway.runtime_settings_bff import guard_governance_change

        client = mock.Mock()
        client.record_decision.side_effect = RuntimeError("memory down")
        with self.assertRaisesRegex(ValueError, "activation aborted"):
            guard_governance_change(
                self._runtime(True),
                self._auth(),
                {"governance_reason": "r"},
                self._canonical(False),
                memory_client=client,
            )


if __name__ == "__main__":
    unittest.main()
