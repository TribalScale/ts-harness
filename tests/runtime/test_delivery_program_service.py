"""Characterization tests for delivery program coordinator."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.delivery_program import (
    WORKFLOW_TYPE_TO_PHASE,
    WORKFLOW_TYPE_TO_PLANNING_TRACK,
    DeliveryProgramService,
    resolve_bridge_target_from_text,
    resolve_workflow_type,
)
from ts_harness.workflows.program_materialize import (
    select_workflow_type,
    snapshot_edges_from,
    snapshot_node,
)

ensure_scripts_importable()
import program_legacy_compat  # noqa: E402


class DeliveryProgramServiceTests(unittest.TestCase):
    def test_phase_order_and_skeleton_mapping(self):
        snap = program_legacy_compat.frozen_legacy_compatibility_topology()
        self.assertEqual(
            tuple(snap["startable_nodes"]),
            ("prd", "jira", "work_orders", "implement"),
        )
        self.assertTrue(snapshot_node(snap, "implement")["terminal"])
        with self.assertRaises(KeyError):
            snapshot_node(snap, "deploy")
        self.assertEqual(
            {
                node["id"]: node["default_workflow_type"]
                for node in snap["nodes"]
            },
            {
                "prd": "prd_request",
                "jira": "jira_from_prd",
                "work_orders": "work_orders_from_prd",
                "implement": "implement_delivery",
            },
        )

    def test_next_phases_graph(self):
        snap = program_legacy_compat.frozen_legacy_compatibility_topology()
        successors = {
            source: tuple(
                str(edge["target"])
                for edge in snapshot_edges_from(snap, source)
            )
            for source in ("prd", "jira", "work_orders", "implement")
        }
        self.assertEqual(set(successors["prd"]), {"jira", "work_orders"})
        self.assertEqual(successors["jira"], ("implement",))
        self.assertEqual(successors["work_orders"], ("implement",))
        self.assertEqual(successors["implement"], ())

    def test_entry_requirements(self):
        snap = program_legacy_compat.frozen_legacy_compatibility_topology()
        self.assertEqual(snapshot_node(snap, "prd")["required_artifacts"], [])
        self.assertEqual(snapshot_node(snap, "jira")["required_artifacts"], ["approved_prd"])
        self.assertEqual(
            snapshot_node(snap, "work_orders")["required_artifacts"],
            ["approved_prd"],
        )
        self.assertEqual(snapshot_node(snap, "implement")["required_artifacts"], [])

    def test_validate_entry_requires_artifacts(self):
        service = DeliveryProgramService(client=MagicMock())
        self.assertEqual(service.validate_entry("prd", {}), [])
        self.assertGreater(len(service.validate_entry("jira", {})), 0)
        self.assertEqual(service.validate_entry("jira", {"approved_prd": {"title": "x"}}), [])
        self.assertGreater(len(service.validate_entry("work_orders", {})), 0)
        self.assertEqual(
            service.validate_entry("work_orders", {"approved_prd": {"title": "x"}}),
            [],
        )
        self.assertGreater(len(service.validate_entry("deploy", {})), 0)
        self.assertGreater(len(service.validate_entry("unknown", {})), 0)

    def test_validate_implement_entry_branches(self):
        service = DeliveryProgramService(client=MagicMock())
        self.assertGreater(len(service.validate_entry("implement", {})), 0)
        self.assertEqual(service.validate_entry("implement", {"jira_epic_key": "EPIC-1"}), [])
        self.assertEqual(
            service.validate_entry(
                "implement",
                {"work_orders_handoff": True, "next_work_order_id": "WO-1"},
            ),
            [],
        )
        self.assertEqual(
            service.validate_entry(
                "implement",
                {"planner_complete": True, "next_work_order_id": "WO-1"},
            ),
            [],
        )
        self.assertGreater(
            len(service.validate_entry("implement", {"work_orders_handoff": True})),
            0,
        )

    def test_resolve_workflow_type_defaults_and_implement_variants(self):
        snap = program_legacy_compat.frozen_legacy_compatibility_topology()
        for node in snap["nodes"]:
            if node["id"] == "implement":
                continue
            self.assertEqual(
                resolve_workflow_type(node["id"], {}),
                node["default_workflow_type"],
            )
            self.assertEqual(
                select_workflow_type(snap, node["id"], {}),
                node["default_workflow_type"],
            )

        self.assertEqual(resolve_workflow_type("implement", {}), "implement_delivery")
        self.assertEqual(
            resolve_workflow_type("implement", {"planning_track": "work_orders"}),
            "implement_work_orders",
        )
        self.assertEqual(
            resolve_workflow_type("implement", {"work_orders_handoff": True}),
            "implement_work_orders",
        )
        self.assertEqual(
            resolve_workflow_type("implement", {"planning_track": "jira"}),
            "implement_delivery",
        )

    def test_workflow_type_reverse_maps(self):
        self.assertEqual(WORKFLOW_TYPE_TO_PHASE["prd_request"], "prd")
        self.assertEqual(WORKFLOW_TYPE_TO_PHASE["implement_work_orders"], "implement")
        self.assertEqual(WORKFLOW_TYPE_TO_PLANNING_TRACK["jira_from_prd"], "jira")
        self.assertEqual(WORKFLOW_TYPE_TO_PLANNING_TRACK["work_orders_from_prd"], "work_orders")

    def test_bridge_signals_fork_to_jira_or_work_orders(self):
        self.assertEqual(
            resolve_bridge_target_from_text("create jira tickets", "prd_request"),
            "jira_from_prd",
        )
        self.assertEqual(
            resolve_bridge_target_from_text("generate work orders", "prd_request"),
            "work_orders_from_prd",
        )
        self.assertEqual(
            resolve_bridge_target_from_text("plan work orders", "prd_request"),
            "work_orders_from_prd",
        )

    def test_bridge_signals_resolve_via_program_snapshot_edges(self):
        from ts_harness.repo_paths import ensure_scripts_importable, programs_dir
        from ts_harness.workflows.program_materialize import (
            node_id_for_workflow_type,
            resolve_bridge_target_nodes_from_text,
        )

        ensure_scripts_importable()
        import program_definitions

        snapshot = program_definitions.program_to_payload(
            program_definitions.load_program(programs_dir() / "software_delivery.json")
        )
        # Pin only jira successor so work-orders signal cannot resolve.
        snapshot["edges"] = [
            edge
            for edge in snapshot["edges"]
            if not (edge["source"] == "prd" and edge["target"] == "work_orders")
        ]
        target_node, target_workflow = resolve_bridge_target_nodes_from_text(
            "create jira tickets",
            source_workflow_type="prd_request",
            snapshot=snapshot,
            source_node="prd",
        )
        self.assertEqual(target_node, "jira")
        self.assertEqual(target_workflow, "jira_from_prd")
        self.assertIsNone(
            resolve_bridge_target_from_text(
                "plan work orders",
                "prd_request",
                snapshot=snapshot,
                source_node="prd",
            )
        )
        self.assertEqual(
            node_id_for_workflow_type(snapshot, "implement_work_orders"),
            "implement",
        )

    def test_create_program_jira_phase(self):
        client = MagicMock()
        client.create_program.return_value = {"id": "prog-1"}
        client.create_workflow.return_value = {"id": "wf-jira"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {
            "program_id": "prog-1",
            "workflow_id": "wf-jira",
        }

        service = DeliveryProgramService(client=client)
        result = service.create_program(
            entry_phase="jira",
            summary="File Jira",
            artifacts={"approved_prd": {"summary": "PRD"}},
        )

        self.assertEqual(result.workflow_type, "jira_from_prd")
        self.assertEqual(result.first_step, "review_prd")
        create_kwargs = client.create_program.call_args.kwargs
        self.assertEqual(result.program_id, create_kwargs["program_id"])
        client.create_program.assert_called_once()
        client.link_workflow_to_program.assert_called_once()

    def test_create_program_survives_related_context_persist_failure(self):
        client = MagicMock()
        client.create_program.return_value = {"id": "prog-1"}
        client.create_workflow.return_value = {"id": "wf-jira"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {
            "program_id": "prog-1",
            "workflow_id": "wf-jira",
        }
        client.patch_program_artifacts.side_effect = RuntimeError("down")

        service = DeliveryProgramService(client=client)
        result = service.create_program(
            entry_phase="jira",
            summary="File Jira",
            artifacts={"approved_prd": {"summary": "PRD"}},
        )

        self.assertEqual(result.workflow_type, "jira_from_prd")
        client.patch_program_artifacts.assert_called()

    def test_create_program_records_link_failures_as_warnings(self):
        client = MagicMock()
        client.create_program.return_value = {"id": "prog-1"}
        client.create_workflow.return_value = {"id": "wf-jira"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {
            "program_id": "prog-1",
            "workflow_id": "wf-jira",
        }
        client.create_program_link.side_effect = ValueError("cross-tenant program link rejected")
        client.list_programs.return_value = {"programs": []}
        client.list_program_links.return_value = {"outbound": [], "inbound": []}

        service = DeliveryProgramService(client=client)
        result = service.create_program(
            entry_phase="jira",
            summary="File Jira",
            artifacts={"approved_prd": {"summary": "PRD"}},
            link_program_ids=["prog-b"],
        )

        self.assertEqual(result.workflow_type, "jira_from_prd")
        artifacts = client.patch_program_artifacts.call_args.args[1]
        warnings = artifacts["related_program_context"]["warnings"]
        self.assertTrue(any("program_link_failed:prog-b" in item for item in warnings))

    def test_advance_program_moves_to_next_phase(self):
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": "prd",
                "summary": "Delivery",
                "metadata": {"artifacts": {"approved_prd": {"ok": True}}},
            },
            "workflows": [],
        }
        client.advance_program.return_value = {"current_phase": "jira"}
        client.create_workflow.return_value = {"id": "wf-jira"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-1"}

        service = DeliveryProgramService(client=client)
        result = service.advance_program(
            program_id="prog-1",
            target_phase="jira",
            summary="Continue to Jira",
            artifacts={"approved_prd": {"ok": True}},
        )

        self.assertEqual(result["target_phase"], "jira")
        self.assertEqual(result["workflow_type"], "jira_from_prd")

    def test_implement_is_terminal_in_topology(self):
        snap = program_legacy_compat.frozen_legacy_compatibility_topology()
        self.assertEqual(snapshot_edges_from(snap, "implement"), [])
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": "implement",
                "summary": "Delivery",
                "metadata": {"artifacts": {"pr_url": "https://example/pr/1"}},
            },
            "workflows": [],
        }
        service = DeliveryProgramService(client=client)
        with self.assertRaises(ValueError):
            service.advance_program(program_id="prog-1", target_phase="prd")

    def test_create_program_pins_software_delivery_snapshot_before_workflow(self):
        client = MagicMock()
        client.create_program.return_value = {"id": "prog-1"}
        client.create_workflow.return_value = {"id": "wf-prd"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-1"}

        service = DeliveryProgramService(client=client)
        result = service.create_program(
            entry_node="prd",
            summary="Start delivery",
            program_type="software_delivery",
        )

        self.assertEqual(result.workflow_type, "prd_request")
        create_kwargs = client.create_program.call_args.kwargs
        self.assertEqual(create_kwargs["program_type"], "software_delivery")
        self.assertEqual(create_kwargs["program_definition_version"], "1.3.0")
        self.assertTrue(create_kwargs["program_definition_revision"])
        snapshot = create_kwargs["program_definition_snapshot"]
        self.assertEqual(snapshot["program_type"], "software_delivery")
        self.assertIn("prd", create_kwargs["resolved_workflow_versions"])
        pin = create_kwargs["resolved_workflow_versions"]["prd"]
        self.assertEqual(pin["workflow_type"], "prd_request")
        self.assertTrue(pin["definition_version"])
        self.assertIn("prd_request", pin.get("pins") or {})
        # Pins must be persisted before the first workflow is created.
        create_prog_order = client.mock_calls.index(
            [c for c in client.mock_calls if c[0] == "create_program"][0]
        )
        create_wf_order = client.mock_calls.index(
            [c for c in client.mock_calls if c[0] == "create_workflow"][0]
        )
        self.assertLess(create_prog_order, create_wf_order)

    def test_advance_uses_pinned_snapshot_not_active_topology(self):
        from ts_harness.repo_paths import programs_dir
        from ts_harness.repo_paths import ensure_scripts_importable

        ensure_scripts_importable()
        import program_definitions

        snapshot = program_definitions.program_to_payload(
            program_definitions.load_program(programs_dir() / "software_delivery.json")
        )
        # Mutate active-looking graph would differ; instance keeps jira-only successor.
        snapshot["edges"] = [
            edge
            for edge in snapshot["edges"]
            if not (edge["source"] == "prd" and edge["target"] == "work_orders")
        ]
        client = MagicMock()
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": "prd",
                "summary": "Delivery",
                "program_definition_snapshot": snapshot,
                "resolved_workflow_versions": {
                    "jira": {
                        "workflow_type": "jira_from_prd",
                        "definition_version": "2.0.0",
                        "pins": {"jira_from_prd": "2.0.0"},
                    }
                },
                "metadata": {"artifacts": {"approved_prd": {"ok": True}}},
            },
            "workflows": [],
        }
        client.advance_program.return_value = {"current_phase": "jira"}
        client.create_workflow.return_value = {"id": "wf-jira"}
        client.create_task.return_value = {"id": "task-1"}
        client.link_workflow_to_program.return_value = {"program_id": "prog-1"}

        service = DeliveryProgramService(client=client)
        with self.assertRaises(ValueError):
            service.advance_program(
                program_id="prog-1",
                target_phase="work_orders",
                summary="blocked by snapshot",
            )
        result = service.advance_program(
            program_id="prog-1",
            target_phase="jira",
            summary="Continue to Jira",
        )
        self.assertEqual(result["workflow_type"], "jira_from_prd")
        self.assertEqual(
            client.create_workflow.call_args.kwargs["definition_version"],
            "2.0.0",
        )

    def test_resolve_node_workflow_uses_variants_and_pins(self):
        from ts_harness.workflows.program_materialize import resolve_node_workflow
        from ts_harness.repo_paths import programs_dir, ensure_scripts_importable

        ensure_scripts_importable()
        import program_definitions

        snapshot = program_definitions.program_to_payload(
            program_definitions.load_program(programs_dir() / "software_delivery.json")
        )
        resolved = {
            "implement": {
                "workflow_type": "implement_delivery",
                "definition_version": "1.2.0",
                "pins": {
                    "implement_delivery": "1.2.0",
                    "implement_work_orders": "1.3.0",
                },
            }
        }
        wf, ver = resolve_node_workflow(snapshot, "implement", {}, resolved)
        self.assertEqual(wf, "implement_delivery")
        self.assertEqual(ver, "1.2.0")
        wf, ver = resolve_node_workflow(
            snapshot,
            "implement",
            {"planning_track": "work_orders"},
            resolved,
        )
        self.assertEqual(wf, "implement_work_orders")
        self.assertEqual(ver, "1.3.0")

    def test_on_workflow_terminal_uses_snapshot_completion_adapter(self):
        from ts_harness.repo_paths import programs_dir, ensure_scripts_importable

        ensure_scripts_importable()
        import program_definitions

        snapshot = program_definitions.program_to_payload(
            program_definitions.load_program(programs_dir() / "software_delivery.json")
        )
        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {
                "id": "wf-prd",
                "workflow_type": "prd_request",
                "program_id": "prog-1",
                "program_phase": "prd",
                "tenant_id": "default",
            },
            "tasks": [
                {
                    "status": "succeeded",
                    "step_name": "finalize_prd",
                    "output": {"approved_prd": {"title": "Done"}},
                }
            ],
        }
        client.get_program.return_value = {
            "program": {
                "id": "prog-1",
                "current_phase": "prd",
                "program_definition_snapshot": snapshot,
                "metadata": {"artifacts": {}},
            }
        }
        service = DeliveryProgramService(client=client)
        result = service.on_workflow_terminal("wf-prd")
        self.assertEqual(result["program_id"], "prog-1")
        self.assertIn("approved_prd", result["artifacts_patched"])


if __name__ == "__main__":
    unittest.main()
