"""Programs that may end after a node: succeeded where they stop, reopened on continue."""

from __future__ import annotations

import copy
import importlib.util
import uuid
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ts_harness.repo_paths import ensure_scripts_importable, programs_dir
from ts_harness.workflows.delivery_program import DeliveryProgramService, ensure_program_can_advance
from ts_harness.workflows.program_definition_validation import validate_program_definition
from ts_harness.workflows.program_materialize import may_end_offer, node_may_end

ensure_scripts_importable()
import program_definitions  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _snapshot() -> dict[str, Any]:
    return program_definitions.program_to_payload(
        program_definitions.load_program(programs_dir() / "software_delivery.json")
    )


def _issue_codes(payload: dict[str, Any]) -> set[str]:
    from ts_harness.workflows.materialize import build_workflow_catalog
    from ts_harness.workflows.program_adapters import build_default_adapter_registry

    _definition, issues = validate_program_definition(
        payload,
        workflow_catalog=build_workflow_catalog(),
        adapter_names=set(build_default_adapter_registry().names()),
        built_in_types={"software_delivery"},
        allow_builtin_type=True,
    )
    return {issue.code for issue in issues}


class DefinitionTests(unittest.TestCase):
    def test_may_end_round_trips_and_defaults_off(self):
        payload = _snapshot()
        by_id = {node["id"]: node for node in payload["nodes"]}
        self.assertTrue(by_id["prd"]["may_end"])
        self.assertNotIn("may_end", by_id["implement"])
        self.assertTrue(node_may_end(payload, "prd"))
        self.assertFalse(node_may_end(payload, "work_orders"))
        self.assertFalse(node_may_end(payload, "missing"))

    def test_may_end_must_be_a_boolean(self):
        payload = _snapshot()
        payload["nodes"][0]["may_end"] = "yes"
        with self.assertRaisesRegex(ValueError, "may_end must be a boolean"):
            program_definitions.load_program_payload(payload)

    def test_builtin_definition_has_no_may_end_issues(self):
        codes = _issue_codes(_snapshot())
        self.assertNotIn("may_end_on_terminal", codes)
        self.assertNotIn("may_end_with_automatic_edge", codes)

    def test_may_end_is_refused_on_terminal_and_automatic_nodes(self):
        payload = _snapshot()
        for node in payload["nodes"]:
            if node["id"] in {"release", "work_orders"}:
                node["may_end"] = True
        codes = _issue_codes(payload)
        self.assertIn("may_end_on_terminal", codes)
        self.assertIn("may_end_with_automatic_edge", codes)


class OfferTests(unittest.TestCase):
    def test_prd_offer_lists_the_phrases_routing_matches(self):
        offer = may_end_offer(_snapshot(), "prd")
        self.assertEqual(
            [(step["target_node"], step["say"]) for step in offer["next_steps"]],
            [("jira", "create jira"), ("work_orders", "work orders")],
        )
        self.assertIn("The PRD step is done, so this program is complete.", offer["text"])
        self.assertIn('"create jira"', offer["text"])

    def test_jira_offer_names_implementation(self):
        offer = may_end_offer(_snapshot(), "jira")
        self.assertEqual(offer["next_steps"][0]["target_node"], "implement")
        self.assertIn('"start implementation"', offer["text"])

    def test_node_without_phrases_points_to_the_console(self):
        snapshot = _snapshot()
        for edge in snapshot["edges"]:
            if edge["source"] == "jira":
                edge["signals"] = []
        offer = may_end_offer(snapshot, "jira")
        self.assertEqual(offer["next_steps"], [])
        self.assertIn("Programs page", offer["text"])

    def test_chat_routing_matches_the_offered_phrase(self):
        from ts_harness.workflows.program_materialize import resolve_bridge_candidates_from_text

        snapshot = _snapshot()
        for node_id in ("prd", "jira"):
            for step in may_end_offer(snapshot, node_id)["next_steps"]:
                found = resolve_bridge_candidates_from_text(step["say"], snapshot=snapshot, source_node=node_id)
                self.assertEqual([item["target_node"] for item in found], [step["target_node"]])


class CompletionTests(unittest.TestCase):
    def _client(self, *, phase: str = "prd", program: dict[str, Any] | None = None, children=None):
        client = mock.MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"id": "wf-1", "program_id": "prog-1", "program_phase": phase, "summary": "login"},
            "tasks": [{"status": "succeeded", "step_name": "approve_prd", "output": {}}],
        }
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": phase,
                "program_definition_snapshot": _snapshot(),
                "metadata": {"artifacts": {}},
                **(program or {}),
            }
        }
        client.get_program_status.return_value = {"children": children or []}
        return client

    def _run(self, client, *, extras: dict[str, Any] | None = None):
        from ts_harness.workflows.hooks import complete_program_phase
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.registry import HookContext

        skeleton = load_skeleton("prd_request")
        ctx = HookContext(
            workflow_id="wf-1",
            workflow_type="prd_request",
            tenant_id="acme",
            task={"id": "gate"},
            stage=skeleton.stages[-1],
            skeleton=skeleton,
            actor="workflow-runner",
            memory_client=client,
            extras=dict(extras or {}),
        )
        service = mock.MagicMock()
        service.on_workflow_terminal.return_value = {"approved_prd": {"title": "Login"}}
        with mock.patch.object(complete_program_phase, "get_delivery_program_service", return_value=service), \
                mock.patch("ts_harness.workflows.hooks.notify_bound_channel.handle") as notify, \
                mock.patch.object(complete_program_phase, "publish_program_brief"):
            result = complete_program_phase.handle(ctx)
        return result, ctx, notify, service

    def _patched_artifacts(self, client) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for call in client.patch_program_artifacts.call_args_list:
            merged.update(call.args[1])
        return merged

    def test_prd_ends_the_program_and_posts_the_offer(self):
        client = self._client()
        result, ctx, notify, service = self._run(client)
        self.assertEqual(result["status"], "phase_completed_program_ended")
        self.assertEqual(result["ended_at_phase"], "prd")
        artifacts = self._patched_artifacts(client)
        self.assertEqual(artifacts["program_status"], "succeeded")
        self.assertEqual(artifacts["ended_at_phase"], "prd")
        client.complete_program.assert_called_once_with("prog-1", actor="workflow-runner", phase="prd")
        service.advance_program.assert_not_called()
        notify.assert_called_once()
        self.assertIn("this program is complete", ctx.extras["channel_text"])
        self.assertEqual(ctx.runner_event, "completed")

    def test_chat_approval_returns_the_offer_instead_of_posting_it(self):
        result, ctx, notify, _service = self._run(self._client(), extras={"reply_in_chat": True})
        self.assertEqual(result["status"], "phase_completed_program_ended")
        self.assertIn('"create jira"', result["offer_text"])
        notify.assert_not_called()
        self.assertNotIn("channel_text", ctx.extras)

    def test_unfinished_children_keep_the_row_open(self):
        client = self._client(children=[{"id": "child-1", "status": "running"}])
        result, _ctx, _notify, _service = self._run(client)
        self.assertEqual(result["status"], "phase_completed_program_ended")
        client.complete_program.assert_not_called()

    def test_child_program_does_not_end_early(self):
        client = self._client(program={"parent_program_id": "parent-1"})
        result, _ctx, notify, _service = self._run(client)
        self.assertEqual(result["status"], "phase_completed")
        client.complete_program.assert_not_called()
        notify.assert_not_called()

    def test_node_without_the_flag_keeps_todays_behavior(self):
        snapshot = _snapshot()
        for node in snapshot["nodes"]:
            node.pop("may_end", None)
        client = self._client(program={"program_definition_snapshot": snapshot})
        result, _ctx, _notify, _service = self._run(client)
        self.assertEqual(result["status"], "phase_completed")
        client.complete_program.assert_not_called()


class ReopenRuleTests(unittest.TestCase):
    def test_rule(self):
        ended = {"ended_at_phase": "prd"}
        self.assertFalse(ensure_program_can_advance({"status": "running", "current_phase": "prd"}, {}))
        self.assertTrue(ensure_program_can_advance({"status": "succeeded", "current_phase": "prd"}, ended))
        with self.assertRaisesRegex(ValueError, "complete"):
            ensure_program_can_advance({"status": "succeeded", "current_phase": "release"}, {})
        with self.assertRaisesRegex(ValueError, "complete"):
            ensure_program_can_advance({"status": "succeeded", "current_phase": "jira"}, ended)
        for status in ("cancelled", "failed"):
            with self.assertRaisesRegex(ValueError, status):
                ensure_program_can_advance({"status": status, "current_phase": "prd"}, ended)

    def _service_client(self, program: dict[str, Any]):
        client = mock.MagicMock()
        client.get_program.return_value = {"program": program, "workflows": []}
        client.advance_program.return_value = {"current_phase": "jira"}
        client.create_workflow.return_value = {"id": "wf-jira"}
        client.create_task.return_value = {"id": "task-1"}
        return client

    def test_continuing_an_ended_program_reopens_it_after_the_advance(self):
        program = {
            "id": "prog-1",
            "status": "succeeded",
            "current_phase": "prd",
            "summary": "Login",
            "program_definition_snapshot": _snapshot(),
            "metadata": {"artifacts": {"approved_prd": {"title": "Login"}, "ended_at_phase": "prd"}},
        }
        client = self._service_client(program)
        order: list[str] = []
        client.advance_program.side_effect = lambda *a, **k: order.append("advance") or {"current_phase": "jira"}
        client.patch_program_artifacts.side_effect = lambda _pid, patch, **k: order.append(
            "reopen" if patch.get("ended_at_phase", "keep") is None else "patch"
        )
        result = DeliveryProgramService(client=client).advance_program(program_id="prog-1", target_phase="jira")
        self.assertEqual(result["workflow_type"], "jira_from_prd")
        self.assertEqual(order[-2:], ["advance", "reopen"])
        reopen = client.patch_program_artifacts.call_args_list[-1].args[1]
        self.assertEqual(reopen["program_status"], "running")
        self.assertEqual(reopen["reopened_from_phase"], "prd")

    def test_a_program_that_shipped_cannot_be_reopened(self):
        program = {
            "id": "prog-1",
            "status": "succeeded",
            "current_phase": "release",
            "program_definition_snapshot": _snapshot(),
            "metadata": {"artifacts": {}},
        }
        client = self._service_client(program)
        with self.assertRaisesRegex(ValueError, "start a new request"):
            DeliveryProgramService(client=client).advance_program(program_id="prog-1", target_phase="rollback")
        client.advance_program.assert_not_called()


def _load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_may_end", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


class GatewayReopenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = _load_gateway()

    def _program(self, **fields):
        program = {
            "id": str(uuid.uuid4()),
            "current_phase": "prd",
            "status": "succeeded",
            "program_definition_snapshot": copy.deepcopy(_snapshot()),
            "metadata": {"artifacts": {"ended_at_phase": "prd"}},
        }
        program.update(fields)
        return program

    def _advance(self, program):
        cursor = _Cursor(program)
        conn = _Conn(cursor)
        with mock.patch.object(self.gateway, "record_workflow_decision", return_value="d1"), \
                mock.patch.object(self.gateway, "audit_workflow_event") as audit:
            result = self.gateway.advance_delivery_program(conn, program["id"], {"target_phase": "jira", "actor": "t"})
        return result, conn, audit

    def test_ended_program_reopens_and_is_audited(self):
        result, conn, audit = self._advance(self._program())
        self.assertEqual(result["current_phase"], "jira")
        self.assertTrue(conn.committed)
        self.assertIn("delivery_program_reopened", [call.args[1] for call in audit.call_args_list])

    def test_closed_programs_are_refused(self):
        for program in (
            self._program(metadata={"artifacts": {}}),
            self._program(status="cancelled"),
            self._program(status="failed"),
        ):
            with self.assertRaises(ValueError):
                self._advance(program)

    def test_running_program_is_unaffected(self):
        _result, _conn, audit = self._advance(self._program(status="running", metadata={"artifacts": {}}))
        self.assertNotIn("delivery_program_reopened", [call.args[1] for call in audit.call_args_list])


class ChatReplyTests(unittest.TestCase):
    def test_approval_reply_carries_the_offer(self):
        from ts_harness.gateway.channel_confirmations import format_workflow_gate_response_ack

        result = {
            "status": "approved",
            "phase": {"status": "phase_completed_program_ended", "offer_text": "The PRD step is done."},
        }
        self.assertEqual(
            format_workflow_gate_response_ack(result, workflow_type="prd_request"),
            "Approved. The PRD step is done.",
        )

    def test_legacy_programs_keep_the_old_reply(self):
        from ts_harness.gateway.channel_confirmations import format_workflow_gate_response_ack

        message = format_workflow_gate_response_ack(
            {"status": "approved", "phase": {"status": "phase_completed"}}, workflow_type="prd_request"
        )
        self.assertTrue(message.startswith("PRD approved. How would you like to continue"))

    def test_handle_approve_marks_the_context_as_a_chat_reply(self):
        from ts_harness.workflows.engine import WorkflowEngine
        from ts_harness.workflows.hooks import build_default_registry

        engine = WorkflowEngine(client=mock.MagicMock(), registry=build_default_registry())
        seen: dict[str, Any] = {}

        def complete(ctx):
            seen.update(ctx.extras)
            return {"status": "phase_completed"}

        with mock.patch.object(engine.registry, "invoke", return_value={}), \
                mock.patch.object(engine, "_maybe_complete_program_phase", side_effect=complete), \
                mock.patch.object(engine, "_maybe_notify"), mock.patch.object(engine, "_sync_session"):
            engine.handle_approve(
                workflow_id="wf-1",
                gate_task={"id": "g", "workflow_type": "prd_request", "step_name": "approve_prd"},
                actor="orchestrator",
                reply_in_chat=True,
            )
        self.assertTrue(seen["reply_in_chat"])


if __name__ == "__main__":
    unittest.main()
