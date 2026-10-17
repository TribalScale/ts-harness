"""Frozen legacy compatibility topology for pre-migration program rows.

This payload is intentionally a static literal. It is the only remaining
hardcoded program graph and exists solely for delivery_program rows that
were created before definition snapshots were pinned.

Do not treat this module as the live source of truth for new programs.
Active topology lives in ``config/programs/software_delivery.json`` (and
managed program definitions). Parity tests keep this payload aligned with
the built-in document so compatibility cannot silently drift.
"""

from __future__ import annotations

import copy
from typing import Any


FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY: dict[str, Any] = {
    "program_type": "software_delivery",
    "definition_version": "legacy-compat",
    "display_name": "Software delivery (legacy compatibility)",
    "startable_nodes": ["prd", "jira", "work_orders", "implement"],
    "nodes": [
        {
            "id": "prd",
            "default_workflow_type": "prd_request",
            "workflow_variants": [],
            "required_artifacts": [],
            "produces_artifacts": ["approved_prd"],
            "input_bindings": [
                {"kind": "program_summary", "target": "summary"},
                {"kind": "transcript_context", "target": "context", "limit": 20},
            ],
            "completion_adapter": "extract_prd_outputs",
            "terminal": False,
        },
        {
            "id": "jira",
            "default_workflow_type": "jira_from_prd",
            "workflow_variants": [],
            "required_artifacts": ["approved_prd"],
            "produces_artifacts": ["jira_epic_key", "planning_track"],
            "input_bindings": [
                {"kind": "program_summary", "target": "summary"},
                {"kind": "artifact", "name": "approved_prd", "target": "approved_prd"},
            ],
            "completion_adapter": "extract_jira_outputs",
            "terminal": False,
        },
        {
            "id": "work_orders",
            "default_workflow_type": "work_orders_from_prd",
            "workflow_variants": [],
            "required_artifacts": ["approved_prd"],
            "produces_artifacts": [
                "planner_complete",
                "work_orders_handoff",
                "next_work_order_id",
                "work_order_ids",
                "work_orders_manifest_path",
                "work_orders_markdown",
                "refinery_artifacts",
                "foundry_artifacts",
                "planner_artifacts",
                "planning_track",
            ],
            "input_bindings": [
                {"kind": "program_summary", "target": "summary"},
                {"kind": "artifact", "name": "approved_prd", "target": "approved_prd"},
                {"kind": "static", "target": "stage", "value": "refinery"},
                {
                    "kind": "artifact",
                    "name": "completed_work_order_ids",
                    "target": "completed_work_order_ids",
                },
            ],
            "completion_adapter": "extract_work_orders_outputs",
            "terminal": False,
        },
        {
            "id": "implement",
            "default_workflow_type": "implement_delivery",
            "workflow_variants": [
                {
                    "workflow_type": "implement_work_orders",
                    "when": {"planning_track": "work_orders"},
                },
                {
                    "workflow_type": "implement_work_orders",
                    "when": {"work_orders_handoff": True},
                },
            ],
            "required_artifacts": [],
            "produces_artifacts": [
                "pr_url",
                "pr_urls",
                "qa_status",
                "qa_summary",
                "qa_evidence",
                "completed_work_order_ids",
            ],
            "input_bindings": [
                {"kind": "program_summary", "target": "summary"},
                {"kind": "artifact", "name": "jira_epic_key", "target": "jira_epic_key"},
                {
                    "kind": "artifact",
                    "name": "jira_story_keys",
                    "target": "jira_story_keys",
                },
                {
                    "kind": "artifact",
                    "name": "next_work_order_id",
                    "target": "work_order_id",
                },
                {"kind": "artifact", "name": "work_order_ref", "target": "work_order_ref"},
                {
                    "kind": "artifact",
                    "name": "work_orders_manifest_path",
                    "target": "work_orders_manifest_path",
                },
                {
                    "kind": "artifact",
                    "name": "completed_work_order_ids",
                    "target": "completed_work_order_ids",
                },
                {
                    "kind": "artifact",
                    "name": "work_orders_markdown",
                    "target": "work_orders_markdown",
                },
                {
                    "kind": "artifact",
                    "name": "refinery_artifacts",
                    "target": "refinery_artifacts",
                },
                {
                    "kind": "artifact",
                    "name": "foundry_artifacts",
                    "target": "foundry_artifacts",
                },
                {
                    "kind": "artifact",
                    "name": "planner_artifacts",
                    "target": "planner_artifacts",
                },
            ],
            "completion_adapter": "extract_implement_outputs",
            "terminal": True,
            "reentry": {
                "target_node": "implement",
                "next_item_artifact": "next_work_order_id",
                "completed_list_artifact": "completed_work_order_ids",
                "remaining_list_artifact": "work_order_ids",
            },
            "complete_channel_text": (
                "All work orders are delivered and merged. Implement is complete.\n\n"
                "CI/CD on the target branch will pick up the merges — no separate deploy step from me."
            ),
        },
    ],
    "edges": [
        {
            "source": "prd",
            "target": "jira",
            "mode": "manual",
            "confirm_required": True,
            "signals": ["create jira", "file tickets", "jira"],
            "artifact_patch": {"planning_track": "jira"},
        },
        {
            "source": "prd",
            "target": "work_orders",
            "mode": "manual",
            "confirm_required": True,
            "signals": ["work orders", "plan work orders", "start work orders"],
            "artifact_patch": {"planning_track": "work_orders"},
        },
        {
            "source": "jira",
            "target": "implement",
            "mode": "manual",
            "confirm_required": False,
            "signals": [],
            "artifact_patch": {"planning_track": "jira"},
        },
        {
            "source": "work_orders",
            "target": "implement",
            "mode": "automatic",
            "confirm_required": False,
            "signals": [],
            "artifact_patch": {"planning_track": "work_orders"},
        },
    ],
}


def frozen_legacy_compatibility_topology() -> dict[str, Any]:
    """Return a deep copy of the frozen legacy compatibility payload."""
    return copy.deepcopy(FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY)


def legacy_compat_topology_view() -> dict[str, Any]:
    """Structural view used by parity tests (ignores version/display metadata)."""
    snapshot = frozen_legacy_compatibility_topology()
    return {
        "startable_nodes": list(snapshot["startable_nodes"]),
        "nodes": [
            {
                "id": node["id"],
                "default_workflow_type": node["default_workflow_type"],
                "workflow_variants": list(node.get("workflow_variants") or []),
                "required_artifacts": list(node.get("required_artifacts") or []),
                "terminal": bool(node.get("terminal")),
            }
            for node in snapshot["nodes"]
        ],
        "edges": [
            {
                "source": edge["source"],
                "target": edge["target"],
                "mode": edge.get("mode"),
                "confirm_required": bool(edge.get("confirm_required")),
                "signals": list(edge.get("signals") or []),
                "artifact_patch": dict(edge.get("artifact_patch") or {}),
            }
            for edge in snapshot["edges"]
        ],
    }
