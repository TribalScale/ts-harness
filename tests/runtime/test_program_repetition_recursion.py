"""Scheduled program starts (repetition) and list-mode node reentry (recursion)."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway import system_jobs
from ts_harness.gateway.cron_store import load_cron_file, save_cron_file, validate_cron_job
from ts_harness.gateway.scheduler import Scheduler
from ts_harness.memory.client import ProgramNotFound
from ts_harness.workflows.delivery_program import DeliveryProgramService
from ts_harness.workflows.hooks import complete_program_phase
from ts_harness.workflows.program_definition_validation import validate_program_definition
from ts_harness.workflows.program_materialize import (
    next_list_item,
    reentry_keys,
    reentry_max_iterations,
    reentry_mode,
    seed_list_reentry,
    validate_entry_against_snapshot,
)
from ts_harness.workflows.registry import HookContext

REPO_ROOT = Path(__file__).resolve().parents[2]


def loop_snapshot(*, terminal: bool = True, max_iterations: int | None = None) -> dict[str, Any]:
    reentry: dict[str, Any] = {
        "target_node": "summarize",
        "remaining_list_artifact": "account_ids",
        "next_item_artifact": "account_id",
    }
    if max_iterations is not None:
        reentry["max_iterations"] = max_iterations
    nodes: list[dict[str, Any]] = [
        {
            "id": "collect",
            "default_workflow_type": "prd_request",
            "workflow_variants": [],
            "required_artifacts": [],
            "produces_artifacts": ["account_ids"],
            "input_bindings": [],
            "completion_adapter": "extract_declared_outputs",
            "terminal": False,
        },
        {
            "id": "summarize",
            "default_workflow_type": "prd_request",
            "workflow_variants": [],
            "required_artifacts": ["account_id"],
            "produces_artifacts": ["account_summary"],
            "input_bindings": [],
            "completion_adapter": "extract_declared_outputs",
            "terminal": terminal,
            "reentry": reentry,
        },
    ]
    edges: list[dict[str, Any]] = [
        {"source": "collect", "target": "summarize", "mode": "automatic", "confirm_required": False, "signals": []},
    ]
    if not terminal:
        nodes.append(
            {
                "id": "report",
                "default_workflow_type": "prd_request",
                "workflow_variants": [],
                "required_artifacts": [],
                "produces_artifacts": [],
                "input_bindings": [],
                "completion_adapter": "extract_declared_outputs",
                "terminal": True,
            }
        )
        edges.append(
            {"source": "summarize", "target": "report", "mode": "automatic", "confirm_required": False, "signals": []}
        )
    return {
        "program_type": "account_digest",
        "definition_version": "1.0.0",
        "display_name": "Account digest",
        "startable_nodes": ["collect"],
        "nodes": nodes,
        "edges": edges,
    }


class FakeMemory:
    """Memory client double that keeps program artifacts across patches."""

    def __init__(self, snapshot: dict[str, Any], artifacts: dict[str, Any]):
        self.program = {
            "id": "prog-1",
            "current_phase": "summarize",
            "tenant_id": "default",
            "program_definition_snapshot": snapshot,
            "metadata": {"artifacts": dict(artifacts)},
        }
        self.patches: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []

    @property
    def artifacts(self) -> dict[str, Any]:
        return self.program["metadata"]["artifacts"]

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return {
            "workflow": {
                "id": workflow_id,
                "program_id": "prog-1",
                "program_phase": "summarize",
                "summary": "digest",
                "source_channel": "slack:C1:111.1",
            },
            "tasks": [{"status": "succeeded", "output": {}}],
        }

    def get_program(self, program_id: str) -> dict[str, Any]:
        return {"program": copy.deepcopy(self.program)}

    def patch_program_artifacts(self, program_id, artifacts, *, merge=True, actor=""):
        self.patches.append(dict(artifacts))
        self.artifacts.update(artifacts)
        return {}

    def cancel_program(self, program_id, *, actor, reason="", cascade=True):
        self.cancelled.append({"program_id": program_id, "reason": reason})
        return {}

    def complete_workflow(self, workflow_id, **kwargs):
        self.completed.append({"workflow_id": workflow_id, **kwargs})
        return {}

    def __getattr__(self, name):
        return MagicMock(return_value={})


def run_hook(memory: FakeMemory, service: Any) -> dict[str, Any]:
    ctx = HookContext(
        workflow_id="wf-1",
        workflow_type="prd_request",
        tenant_id="default",
        task={"id": "t-final"},
        stage=None,
        skeleton=SimpleNamespace(program={}),
        actor="workflow-runner",
        memory_client=memory,
        extras={},
    )
    with patch(
        "ts_harness.workflows.hooks.complete_program_phase.get_delivery_program_service",
        return_value=service,
    ), patch("ts_harness.workflows.hooks.complete_program_phase._notify_channel") as notify:
        result = complete_program_phase.handle(ctx)
    result["_notify"] = notify
    return result


def loop_service() -> MagicMock:
    service = MagicMock()
    service.on_workflow_terminal.return_value = {}
    service.advance_program.return_value = {"workflow_id": "wf-2"}
    return service


class ReentryHelperTests(unittest.TestCase):
    def test_mode_and_defaults(self):
        work_orders = {"target_node": "implement", "remaining_list_artifact": "work_order_ids"}
        self.assertEqual(reentry_mode(work_orders), "work_orders")
        self.assertIsNone(reentry_max_iterations(work_orders))
        listed = {"target_node": "s", "remaining_list_artifact": "account_ids"}
        self.assertEqual(reentry_mode(listed), "list")
        self.assertEqual(reentry_keys(listed), ("account_ids", "completed_account_ids", "next_item"))
        self.assertEqual(reentry_max_iterations(listed), 50)

    def test_next_item_skips_completed_and_uses_dict_ids(self):
        reentry = {"remaining_list_artifact": "items"}
        artifacts = {"items": [{"id": "a"}, {"id": "b"}], "completed_items": ["a"]}
        self.assertEqual(next_list_item(artifacts, reentry), {"id": "b"})

    def test_seed_sets_first_item_and_resets_counter(self):
        seeded = seed_list_reentry(
            loop_snapshot(),
            "summarize",
            {"account_ids": ["A", "B"], "reentry_iterations": {"summarize": 4}},
        )
        self.assertEqual(seeded["account_id"], "A")
        self.assertEqual(seeded["reentry_iterations"], {"summarize": 0})

    def test_empty_list_is_rejected_on_entry(self):
        snapshot = loop_snapshot()
        snapshot["startable_nodes"] = ["summarize"]
        seeded = seed_list_reentry(snapshot, "summarize", {"account_ids": []})
        errors = validate_entry_against_snapshot(snapshot, "summarize", seeded, require_startable=True)
        self.assertTrue(any("account_id" in error for error in errors))


class AdvanceProgramReentryTests(unittest.TestCase):
    def _service(self, snapshot, current_phase, artifacts):
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": current_phase,
                "program_definition_snapshot": snapshot,
                "binding_key": "cron:digest",
                "source_channel": "slack:C1:111.1",
                "metadata": {"artifacts": artifacts},
            }
        }
        return DeliveryProgramService(client=client), client

    def test_self_reentry_requires_declaration(self):
        snapshot = loop_snapshot()
        service, _ = self._service(snapshot, "collect", {})
        with self.assertRaises(ValueError):
            service.advance_program(program_id="prog-1", target_phase="collect")

    def test_edge_entry_seeds_item_and_uses_program_source_channel(self):
        snapshot = loop_snapshot()
        service, client = self._service(snapshot, "collect", {"account_ids": ["A", "B"]})
        with patch("ts_harness.workflows.delivery_program.load_pinned_skeleton"), patch(
            "ts_harness.workflows.delivery_program.materialize_program_workflow",
            return_value=("wf-2", ["t"], "first"),
        ) as materialize, patch(
            "ts_harness.workflows.delivery_program.build_intake_for_node", return_value={}
        ):
            service.advance_program(program_id="prog-1", target_phase="summarize")
        patched = client.patch_program_artifacts.call_args.args[1]
        self.assertEqual(patched["account_id"], "A")
        self.assertEqual(materialize.call_args.kwargs["binding_key"], "slack:C1:111.1")


class ListLoopHookTests(unittest.TestCase):
    def test_reenters_with_next_item_and_records_completed(self):
        memory = FakeMemory(loop_snapshot(), {"account_ids": ["A", "B", "C"], "account_id": "A"})
        service = loop_service()
        result = run_hook(memory, service)
        self.assertEqual(result["status"], "phase_reentered")
        self.assertEqual(memory.artifacts["completed_account_ids"], ["A"])
        kwargs = service.advance_program.call_args.kwargs
        self.assertEqual(kwargs["target_phase"], "summarize")
        self.assertEqual(kwargs["artifacts"], {"account_id": "B", "reentry_index": 1})
        self.assertNotIn("work_orders_handoff", kwargs["artifacts"])
        self.assertEqual(kwargs["binding_key"], "slack:C1:111.1")
        self.assertEqual(memory.completed[0]["superseded_by"], "wf-2")

    def test_three_items_then_program_succeeds(self):
        memory = FakeMemory(loop_snapshot(), {"account_ids": ["A", "B", "C"], "account_id": "A"})
        statuses = []
        for _ in range(3):
            service = loop_service()
            statuses.append(run_hook(memory, service)["status"])
            if service.advance_program.called:
                memory.artifacts.update(service.advance_program.call_args.kwargs["artifacts"])
        self.assertEqual(statuses, ["phase_reentered", "phase_reentered", "phase_completed_loop_done"])
        self.assertEqual(memory.artifacts["completed_account_ids"], ["A", "B", "C"])
        self.assertEqual(memory.artifacts["program_status"], "succeeded")
        self.assertTrue(memory.artifacts["summarize_loop_complete"])
        self.assertNotIn("work_orders_delivery_complete", memory.artifacts)

    def test_completed_list_does_not_duplicate(self):
        memory = FakeMemory(
            loop_snapshot(),
            {"account_ids": ["A", "B"], "account_id": "A", "completed_account_ids": ["A"]},
        )
        run_hook(memory, loop_service())
        self.assertEqual(memory.artifacts["completed_account_ids"], ["A"])

    def test_list_done_follows_automatic_edge(self):
        memory = FakeMemory(loop_snapshot(terminal=False), {"account_ids": ["A"], "account_id": "A"})
        service = loop_service()
        with patch(
            "ts_harness.workflows.hooks.complete_program_phase._auto_advance",
            return_value={"status": "auto_advanced"},
        ) as auto:
            result = run_hook(memory, service)
        self.assertEqual(result["status"], "auto_advanced")
        self.assertEqual(auto.call_args.args[6]["target"], "report")
        self.assertTrue(memory.artifacts["summarize_loop_complete"])

    def test_cap_cancels_program(self):
        memory = FakeMemory(
            loop_snapshot(max_iterations=2),
            {"account_ids": ["A", "B", "C"], "account_id": "B", "reentry_iterations": {"summarize": 1}},
        )
        service = loop_service()
        result = run_hook(memory, service)
        self.assertEqual(result["status"], "phase_reentry_capped")
        self.assertEqual(memory.cancelled, [{"program_id": "prog-1", "reason": "reentry_max_iterations"}])
        service.advance_program.assert_not_called()
        result["_notify"].assert_called_once()


class ReentryValidationTests(unittest.TestCase):
    catalogs: ClassVar[dict[str, Any]] = {
        "workflow_catalog": {
            "prd_request": {
                "workflow_type": "prd_request",
                "status": "built_in",
                "available_versions": ["1.3.0"],
                "resolvable": True,
                "active_version": "1.3.0",
                "contract_output_keys": set(),
            }
        },
        "adapter_names": {"extract_declared_outputs"},
        "built_in_types": {"software_delivery"},
    }

    def codes(self, payload):
        _, issues = validate_program_definition(payload, **self.catalogs)
        return {(issue.severity, issue.code) for issue in issues}

    def test_valid_loop_has_no_reentry_issues(self):
        codes = self.codes(loop_snapshot())
        self.assertFalse({code for _, code in codes if code.startswith("reentry")})
        self.assertNotIn(("error", "missing_required_artifact"), codes)

    def test_non_self_target_rejected(self):
        payload = loop_snapshot()
        payload["nodes"][1]["reentry"]["target_node"] = "collect"
        self.assertIn(("error", "reentry_not_self"), self.codes(payload))

    def test_loop_producing_own_list_rejected(self):
        payload = loop_snapshot()
        payload["nodes"][1]["produces_artifacts"] = ["account_ids"]
        self.assertIn(("error", "reentry_produces_own_list"), self.codes(payload))

    def test_item_not_required_warns(self):
        payload = loop_snapshot()
        payload["nodes"][1]["required_artifacts"] = []
        self.assertIn(("warning", "reentry_item_not_required"), self.codes(payload))

    def test_bad_max_iterations_and_list_mode_without_list_fail_to_load(self):
        payload = loop_snapshot(max_iterations=501)
        self.assertIn(("error", "schema_load_error"), self.codes(payload))
        payload = loop_snapshot()
        payload["nodes"][1]["reentry"] = {"target_node": "summarize", "mode": "list"}
        self.assertIn(("error", "schema_load_error"), self.codes(payload))

    def test_software_delivery_still_valid(self):
        import json

        payload = json.loads((REPO_ROOT / "config" / "programs" / "software_delivery.json").read_text())
        _, issues = validate_program_definition(
            payload,
            workflow_catalog={},
            adapter_names=set(),
            built_in_types={"software_delivery"},
            allow_builtin_type=True,
        )
        self.assertFalse([issue for issue in issues if issue.code.startswith("reentry")])


class ScheduledProgramHandlerTests(unittest.TestCase):
    payload: ClassVar[dict[str, Any]] = {
        "name": "digest",
        "kind": "system",
        "handler": "start_program",
        "program": {"program_type": "account_digest", "entry_node": "collect", "summary": "Weekly digest"},
    }

    def _memory(self, *, found=None, status="succeeded", error=None):
        memory = MagicMock()
        if error is not None:
            memory.find_program_by_binding.side_effect = error
        else:
            memory.find_program_by_binding.return_value = {"program": found} if found else {}
        memory.get_program_status.return_value = {"status": status, "waiting_on": {"kind": "approval"}}
        return memory

    def _service(self):
        service = MagicMock()
        service.create_program.return_value = SimpleNamespace(program_id="prog-new", workflow_id="wf-new")
        return service

    def test_starts_when_no_previous_run(self):
        service = self._service()
        result = system_jobs.start_scheduled_program(
            self.payload, memory=self._memory(error=ProgramNotFound("program not found")), service=service
        )
        self.assertEqual(result["status"], "started")
        kwargs = service.create_program.call_args.kwargs
        self.assertEqual(kwargs["binding_key"], "cron:digest")
        self.assertIsNone(kwargs["source_channel"])
        self.assertEqual(kwargs["requester_actor_id"], "scheduler")
        self.assertEqual(kwargs["artifacts"]["schedule_name"], "digest")

    def test_gateway_400_not_found_also_starts(self):
        service = self._service()
        result = system_jobs.start_scheduled_program(
            self.payload, memory=self._memory(error=RuntimeError('{"error": "program not found"}')), service=service
        )
        self.assertEqual(result["status"], "started")

    def test_skips_while_previous_run_open(self):
        for status in ("running", "blocked"):
            service = self._service()
            result = system_jobs.start_scheduled_program(
                self.payload, memory=self._memory(found={"id": "prog-old"}, status=status), service=service
            )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "previous_run_open")
            self.assertEqual(result["program_id"], "prog-old")
            service.create_program.assert_not_called()

    def test_starts_when_previous_run_derived_succeeded(self):
        service = self._service()
        result = system_jobs.start_scheduled_program(
            self.payload, memory=self._memory(found={"id": "prog-old"}, status="succeeded"), service=service
        )
        self.assertEqual(result["status"], "started")

    def test_lookup_error_fails_closed(self):
        service = self._service()
        result = system_jobs.start_scheduled_program(
            self.payload, memory=self._memory(error=RuntimeError("connection refused")), service=service
        )
        self.assertEqual(result, {"status": "skipped", "reason": "guard_lookup_failed", "error": "connection refused"})
        service.create_program.assert_not_called()

    def test_create_error_is_reported(self):
        service = self._service()
        service.create_program.side_effect = ValueError("program definition unavailable for start: nope")
        result = system_jobs.start_scheduled_program(
            self.payload, memory=self._memory(error=ProgramNotFound("x")), service=service
        )
        self.assertEqual(result["status"], "error")

    def test_notify_binds_run_to_slack_thread(self):
        service = self._service()
        notifier = MagicMock()
        notifier.post_thread_text.return_value = {"ok": True, "ts": "171.5"}
        payload = copy.deepcopy(self.payload)
        payload["program"]["notify"] = {"channel": "C9", "text": "Digest started"}
        result = system_jobs.start_scheduled_program(
            payload, memory=self._memory(error=ProgramNotFound("x")), service=service, notifier=notifier
        )
        notifier.post_thread_text.assert_called_once_with(channel="C9", text="Digest started")
        kwargs = service.create_program.call_args.kwargs
        self.assertEqual(kwargs["binding_key"], "cron:digest")
        self.assertEqual(kwargs["source_channel"], "slack:C9:171.5")
        self.assertEqual(result["notify"]["status"], "posted")

    def test_notify_failure_still_starts(self):
        service = self._service()
        notifier = MagicMock()
        notifier.post_thread_text.return_value = {"ok": False, "error": "no_token"}
        payload = copy.deepcopy(self.payload)
        payload["program"]["notify"] = {"channel": "C9"}
        result = system_jobs.start_scheduled_program(
            payload, memory=self._memory(error=ProgramNotFound("x")), service=service, notifier=notifier
        )
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["notify"], {"status": "failed", "error": "no_token"})
        self.assertIsNone(service.create_program.call_args.kwargs["source_channel"])

    def test_dispatch_passes_payload_only_to_start_program(self):
        handler = MagicMock(return_value={"status": "started"})
        with patch.dict(system_jobs.SYSTEM_HANDLERS, {"start_program": handler}):
            system_jobs.dispatch_system_job(self.payload)
        handler.assert_called_once_with(self.payload)


class CreateProgramSourceChannelTests(unittest.TestCase):
    def _create(self, **kwargs):
        client = MagicMock()
        client.create_program.return_value = {"root_program_id": "prog"}
        service = DeliveryProgramService(client=client)
        with patch("ts_harness.workflows.delivery_program.load_pinned_skeleton"), patch(
            "ts_harness.workflows.delivery_program.materialize_program_workflow",
            return_value=("wf", ["t"], "first"),
        ) as materialize, patch(
            "ts_harness.workflows.program_brief.attach_related_program_context"
        ):
            service.create_program(entry_node="prd", summary="s", pin_definition=False, **kwargs)
        return client.create_program.call_args.kwargs, materialize.call_args.kwargs

    def test_defaults_to_binding_key(self):
        created, materialized = self._create(binding_key="slack:C1:1.0")
        self.assertEqual(created["source_channel"], "slack:C1:1.0")
        self.assertEqual(materialized["binding_key"], "slack:C1:1.0")

    def test_separate_source_channel(self):
        created, materialized = self._create(binding_key="cron:digest", source_channel="slack:C9:2.0")
        self.assertEqual(created["binding_key"], "cron:digest")
        self.assertEqual(created["source_channel"], "slack:C9:2.0")
        self.assertEqual(materialized["binding_key"], "slack:C9:2.0")


class ScheduledCronJobConfigTests(unittest.TestCase):
    def _job(self, **program):
        return {
            "name": "sd-weekly",
            "kind": "system",
            "handler": "start_program",
            "cron": "0 9 * * 1",
            "program": {"program_type": "software_delivery", "entry_node": "prd", "summary": "Weekly", **program},
        }

    def test_valid_job_roundtrips_and_reaches_payload(self):
        job = validate_cron_job(self._job(notify={"channel": "C9"}))
        self.assertEqual(job["agent_id"], "ops-agent")
        self.assertEqual(job["program"]["notify"], {"channel": "C9"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            save_cron_file([job], path)
            loaded = load_cron_file(path)
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        dispatched: list[dict[str, Any]] = []
        scheduler = Scheduler(config, dispatched.append, cron_jobs=[])
        scheduler.replace_cron_jobs(loaded)
        self.assertEqual(scheduler.list_cron_jobs()[0]["program"]["program_type"], "software_delivery")
        scheduler.trigger_job("sd-weekly")
        self.assertEqual(dispatched[0]["name"], "sd-weekly")
        self.assertEqual(dispatched[0]["program"]["summary"], "Weekly")
        self.assertEqual(dispatched[0]["handler"], "start_program")

    def test_rejections(self):
        cases = [
            self._job(summary=""),
            self._job(program_type="no_such_program"),
            self._job(entry_node="jira"),  # requires approved_prd
            self._job(notify={"channel": "slack:C9"}),
            {**self._job(), "kind": "cron", "agent_id": "ops-agent"},
            {**self._job(), "name": "bad:name"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                validate_cron_job(case)


if __name__ == "__main__":
    unittest.main()
