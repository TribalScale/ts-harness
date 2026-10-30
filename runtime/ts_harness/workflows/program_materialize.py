"""Snapshot-driven helpers for program start, advance, and materialization."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.definition_paths import list_history_paths_for_version
from ts_harness.workflows.materialize import (
    definition_store_root,
    load_skeleton,
    materialize_skeleton_tasks,
)
from ts_harness.workflows.program_adapters import (
    ProgramAdapterRegistry,
    build_default_adapter_registry,
)

ensure_scripts_importable()

import workflow_definitions  # noqa: E402

logger = logging.getLogger(__name__)


def parse_program_snapshot(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        value = json.loads(text)
    if not isinstance(value, dict):
        return None
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return None
    return value


def parse_resolved_workflow_versions(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        value = json.loads(text)
    if not isinstance(value, dict):
        return {}
    return value


def snapshot_node(snapshot: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in list(snapshot.get("nodes") or []):
        if isinstance(node, dict) and str(node.get("id") or "") == node_id:
            return node
    raise KeyError(f"program node not found: {node_id}")


def snapshot_edges_from(snapshot: dict[str, Any], source: str) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for edge in list(snapshot.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        if str(edge.get("source") or "") == source:
            edges.append(edge)
    return edges


def find_edge(
    snapshot: dict[str, Any],
    *,
    source: str,
    target: str,
) -> dict[str, Any] | None:
    for edge in snapshot_edges_from(snapshot, source):
        if str(edge.get("target") or "") == target:
            return edge
    return None


def automatic_edges_from(
    snapshot: dict[str, Any],
    source: str,
) -> list[dict[str, Any]]:
    return [
        edge
        for edge in snapshot_edges_from(snapshot, source)
        if str(edge.get("mode") or "") == "automatic"
    ]


def node_is_terminal(snapshot: dict[str, Any], node_id: str) -> bool:
    try:
        node = snapshot_node(snapshot, node_id)
    except KeyError:
        return False
    return bool(node.get("terminal"))


WORK_ORDER_REENTRY_KEYS = ("work_order_ids", "completed_work_order_ids", "next_work_order_id")
DEFAULT_LIST_MAX_ITERATIONS = 50


def node_config(snapshot: dict[str, Any] | None, node_id: str) -> dict[str, Any]:
    if not node_id or snapshot is None:
        return {}
    try:
        node = snapshot_node(snapshot, node_id)
    except KeyError:
        return {}
    return dict(node) if isinstance(node, dict) else {}


def node_reentry(snapshot: dict[str, Any] | None, node_id: str) -> dict[str, Any]:
    reentry = node_config(snapshot, node_id).get("reentry")
    return dict(reentry) if isinstance(reentry, dict) else {}


def reentry_mode(reentry: dict[str, Any]) -> str:
    mode = str(reentry.get("mode") or "").strip()
    if mode in {"list", "work_orders"}:
        return mode
    remaining = str(reentry.get("remaining_list_artifact") or "").strip()
    return "work_orders" if remaining in {"", WORK_ORDER_REENTRY_KEYS[0]} else "list"


def reentry_keys(reentry: dict[str, Any]) -> tuple[str, str, str]:
    """(remaining_list, completed_list, next_item) artifact names for a reentry config."""
    if reentry_mode(reentry) == "work_orders":
        defaults = WORK_ORDER_REENTRY_KEYS
    else:
        remaining = str(reentry.get("remaining_list_artifact") or "").strip()
        defaults = (remaining, f"completed_{remaining}", "next_item")
    return (
        str(reentry.get("remaining_list_artifact") or defaults[0]).strip(),
        str(reentry.get("completed_list_artifact") or defaults[1]).strip(),
        str(reentry.get("next_item_artifact") or defaults[2]).strip(),
    )


def reentry_max_iterations(reentry: dict[str, Any]) -> int | None:
    raw = reentry.get("max_iterations")
    if raw is None:
        return DEFAULT_LIST_MAX_ITERATIONS if reentry_mode(reentry) == "list" else None
    return int(raw)


def reentry_item_key(item: Any) -> str:
    if isinstance(item, dict) and item.get("id") is not None:
        return str(item["id"])
    return str(item)


def next_list_item(artifacts: dict[str, Any], reentry: dict[str, Any]) -> Any:
    remaining_key, completed_key, _ = reentry_keys(reentry)
    completed = {reentry_item_key(item) for item in list(artifacts.get(completed_key) or [])}
    for item in list(artifacts.get(remaining_key) or []):
        if reentry_item_key(item) not in completed:
            return item
    return None


def seed_list_reentry(
    snapshot: dict[str, Any] | None,
    node_id: str,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    """Set the first pending item and reset the loop counter when a list-mode node is entered."""
    reentry = node_reentry(snapshot, node_id)
    if not reentry or reentry_mode(reentry) != "list":
        return artifacts
    _, _, next_key = reentry_keys(reentry)
    seeded = dict(artifacts)
    item = next_list_item(seeded, reentry)
    if item is None:
        seeded.pop(next_key, None)
    else:
        seeded[next_key] = item
    iterations = dict(seeded.get("reentry_iterations") or {})
    iterations[node_id] = 0
    seeded["reentry_iterations"] = iterations
    return seeded


def node_id_for_workflow_type(
    snapshot: dict[str, Any],
    workflow_type: str,
) -> str | None:
    """Return the program node id that binds the given workflow type."""
    wanted = str(workflow_type or "").strip()
    if not wanted:
        return None
    for node in list(snapshot.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue
        if str(node.get("default_workflow_type") or "").strip() == wanted:
            return node_id
        for variant in list(node.get("workflow_variants") or []):
            if not isinstance(variant, dict):
                continue
            if str(variant.get("workflow_type") or "").strip() == wanted:
                return node_id
    return None


def resolve_bridge_target_nodes_from_text(
    text: str,
    *,
    source_workflow_type: str = "prd_request",
    snapshot: dict[str, Any] | None = None,
    source_node: str | None = None,
    artifacts: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Match edge signals on a program snapshot; return (target_node, workflow_type).

    Returns (None, None) when no edge matches or when more than one distinct
    target matches; callers must then ask the user to choose.
    """
    candidates = resolve_bridge_candidates_from_text(
        text,
        source_workflow_type=source_workflow_type,
        snapshot=snapshot,
        source_node=source_node,
        artifacts=artifacts,
    )
    if len(candidates) != 1:
        return None, None
    only = candidates[0]
    return only["target_node"], only["workflow_type"]


def resolve_bridge_candidates_from_text(
    text: str,
    *,
    source_workflow_type: str = "prd_request",
    snapshot: dict[str, Any] | None = None,
    source_node: str | None = None,
    artifacts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return every distinct outgoing edge whose signals match ``text``."""
    if snapshot is None:
        return []
    current = str(source_node or "").strip()
    if not current:
        current = node_id_for_workflow_type(snapshot, source_workflow_type) or ""
    if not current:
        return []
    lowered = (text or "").lower()
    artifact_bag = dict(artifacts or {})
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for edge in snapshot_edges_from(snapshot, current):
        signals = [str(item).lower() for item in list(edge.get("signals") or []) if str(item).strip()]
        if not signals:
            continue
        matched = [signal for signal in signals if signal in lowered]
        if not matched:
            continue
        target_node = str(edge.get("target") or "").strip()
        if not target_node:
            continue
        patch = edge.get("artifact_patch")
        merged = dict(artifact_bag)
        if isinstance(patch, dict):
            merged.update(patch)
        try:
            workflow_type = select_workflow_type(snapshot, target_node, merged)
        except KeyError:
            workflow_type = ""
        key = (target_node, workflow_type)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "source_node": current,
                "target_node": target_node,
                "workflow_type": workflow_type or None,
                "matched_signals": matched,
                "artifact_patch": dict(patch) if isinstance(patch, dict) else {},
            }
        )
    return candidates


def program_workflow_types_from_definitions(
    definitions: list[dict[str, Any]],
) -> set[str]:
    """Collect workflow types referenced by program definition payloads."""
    types: set[str] = set()
    for payload in definitions:
        if not isinstance(payload, dict):
            continue
        for node in list(payload.get("nodes") or []):
            if not isinstance(node, dict):
                continue
            default = str(node.get("default_workflow_type") or "").strip()
            if default:
                types.add(default)
            for variant in list(node.get("workflow_variants") or []):
                if not isinstance(variant, dict):
                    continue
                workflow_type = str(variant.get("workflow_type") or "").strip()
                if workflow_type:
                    types.add(workflow_type)
    return types


def _variant_matches(when: dict[str, Any], artifacts: dict[str, Any]) -> bool:
    for key, expected in when.items():
        if artifacts.get(key) != expected:
            return False
    return True


def select_workflow_type(
    snapshot: dict[str, Any],
    node_id: str,
    artifacts: dict[str, Any],
) -> str:
    node = snapshot_node(snapshot, node_id)
    for variant in list(node.get("workflow_variants") or []):
        if not isinstance(variant, dict):
            continue
        when = variant.get("when")
        if not isinstance(when, dict) or not when:
            continue
        if _variant_matches(when, artifacts):
            workflow_type = str(variant.get("workflow_type") or "").strip()
            if workflow_type:
                return workflow_type
    return str(node.get("default_workflow_type") or "").strip()


def _pin_version_for(
    resolved: dict[str, Any],
    node_id: str,
    workflow_type: str,
) -> str:
    entry = resolved.get(node_id)
    if not isinstance(entry, dict):
        return ""
    pins = entry.get("pins")
    if isinstance(pins, dict) and workflow_type in pins:
        return str(pins.get(workflow_type) or "").strip()
    if str(entry.get("workflow_type") or "") == workflow_type:
        return str(entry.get("definition_version") or "").strip()
    return ""


def resolve_node_workflow(
    snapshot: dict[str, Any],
    node_id: str,
    artifacts: dict[str, Any],
    resolved_workflow_versions: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return (workflow_type, definition_version) for a node from snapshot pins."""
    workflow_type = select_workflow_type(snapshot, node_id, artifacts)
    if not workflow_type:
        raise ValueError(f"node {node_id} has no resolvable workflow_type")
    resolved = parse_resolved_workflow_versions(resolved_workflow_versions)
    version = _pin_version_for(resolved, node_id, workflow_type)
    if not version:
        # Fall back to currently loadable skeleton version when pins omit it.
        skeleton = load_skeleton(workflow_type)
        version = str(skeleton.definition_version)
    return workflow_type, version


def current_workflow_version(workflow_type: str) -> str:
    skeleton = load_skeleton(workflow_type)
    return str(skeleton.definition_version)


def build_resolved_workflow_versions(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Pin every default and variant workflow option for the snapshot."""
    resolved: dict[str, Any] = {}
    for node in list(snapshot.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue
        default_type = str(node.get("default_workflow_type") or "").strip()
        pins: dict[str, str] = {}
        workflow_types = []
        if default_type:
            workflow_types.append(default_type)
        for variant in list(node.get("workflow_variants") or []):
            if not isinstance(variant, dict):
                continue
            workflow_type = str(variant.get("workflow_type") or "").strip()
            if workflow_type:
                workflow_types.append(workflow_type)
        for workflow_type in workflow_types:
            pins[workflow_type] = current_workflow_version(workflow_type)
        if not default_type:
            continue
        resolved[node_id] = {
            "workflow_type": default_type,
            "definition_version": pins[default_type],
            "pins": pins,
        }
    return resolved


def load_pinned_skeleton(
    workflow_type: str,
    definition_version: str | None = None,
):
    """Load exact built-in/managed/history workflow content for a pin."""
    requested = str(definition_version or "").strip()
    current = load_skeleton(workflow_type)
    if not requested or requested == str(current.definition_version):
        return current

    store_root = definition_store_root()
    if store_root is not None:
        matches = list_history_paths_for_version(
            store_root,
            workflow_type,
            requested,
        )
        if matches:
            skeleton = workflow_definitions.load_skeleton(matches[-1])
            if (
                skeleton.workflow_type == workflow_type
                and str(skeleton.definition_version) == requested
            ):
                return skeleton

    message = (
        "workflow skeleton version not found: "
        f"workflow_type={workflow_type} definition_version={requested}"
    )
    logger.error(message)
    raise LookupError(message)


def validate_entry_against_snapshot(
    snapshot: dict[str, Any],
    entry_node: str,
    artifacts: dict[str, Any],
    *,
    require_startable: bool = True,
) -> list[str]:
    try:
        node = snapshot_node(snapshot, entry_node)
    except KeyError:
        node_ids = [
            str(item.get("id"))
            for item in list(snapshot.get("nodes") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        return [f"entry_phase must be one of: {', '.join(node_ids)}"]

    if require_startable:
        startable = {
            str(item)
            for item in list(snapshot.get("startable_nodes") or [])
            if str(item).strip()
        }
        if entry_node not in startable:
            return [
                f"entry_phase must be one of: {', '.join(sorted(startable))}"
            ]

    required = [str(item) for item in list(node.get("required_artifacts") or [])]
    missing = [key for key in required if not artifacts.get(key)]
    errors: list[str] = []
    if missing:
        errors.append(f"missing required artifacts: {', '.join(missing)}")
    reentry = node.get("reentry") if isinstance(node.get("reentry"), dict) else {}
    if reentry and reentry_mode(reentry) == "list":
        remaining_key, _, next_key = reentry_keys(reentry)
        if artifacts.get(next_key) is None and next_key not in missing:
            errors.append(f"nothing to iterate: {remaining_key} has no pending items")

    # Preserve software-delivery implement gate when required_artifacts is empty.
    if entry_node == "implement":
        errors.extend(validate_implement_prerequisites(artifacts))
    return errors


def validate_implement_prerequisites(artifacts: dict[str, Any]) -> list[str]:
    if artifacts.get("jira_epic_key"):
        return []
    if artifacts.get("work_orders_handoff") and artifacts.get("next_work_order_id"):
        return []
    if artifacts.get("planner_complete") and artifacts.get("next_work_order_id"):
        return []
    if artifacts.get("jira_issue_key"):
        return []
    return [
        "missing implement prerequisites: jira_epic_key, jira_issue_key, or "
        "(planner_complete + next_work_order_id / work_orders_handoff)",
    ]


def resolve_target_node(
    snapshot: dict[str, Any],
    current_node: str,
    target_node: str | None,
    *,
    allow_reentry: bool = False,
) -> str:
    if target_node:
        if allow_reentry and current_node == target_node:
            return target_node
        if find_edge(snapshot, source=current_node, target=target_node) is None:
            allowed = [
                str(edge.get("target"))
                for edge in snapshot_edges_from(snapshot, current_node)
            ]
            raise ValueError(
                f"cannot advance from {current_node} to {target_node}; "
                f"allowed: {', '.join(allowed) or 'none'}",
            )
        return target_node
    successors = [
        str(edge.get("target"))
        for edge in snapshot_edges_from(snapshot, current_node)
        if edge.get("target")
    ]
    # Prefer unique automatic successor, else unique successor.
    auto = [
        str(edge.get("target"))
        for edge in automatic_edges_from(snapshot, current_node)
        if edge.get("target")
    ]
    if len(auto) == 1:
        return auto[0]
    if len(successors) == 1:
        return successors[0]
    raise ValueError(f"explicit target_phase required after {current_node}")


def build_intake_for_node(
    *,
    snapshot: dict[str, Any],
    node_id: str,
    workflow_type: str,
    summary: str,
    artifacts: dict[str, Any],
    transcript_messages: list[dict[str, Any]] | None = None,
    extra_input: dict[str, Any] | None = None,
    source_workflow_id: str | None = None,
    adapters: ProgramAdapterRegistry | None = None,
) -> dict[str, Any]:
    node = snapshot_node(snapshot, node_id)
    adapter_name = str(node.get("completion_adapter") or "").strip()
    registry = adapters or build_default_adapter_registry()
    if adapter_name:
        intake = registry.build_inputs(
            adapter_name,
            summary=summary,
            artifacts=artifacts,
            transcript_messages=transcript_messages,
            extra_input=extra_input,
            workflow_type=workflow_type,
            required_artifacts=list(node.get("required_artifacts") or []),
            produces_artifacts=list(node.get("produces_artifacts") or []),
            snapshot=snapshot,
        )
    else:
        intake = {"summary": summary, **(extra_input or {})}
        for binding in list(node.get("input_bindings") or []):
            if not isinstance(binding, dict):
                continue
            kind = str(binding.get("kind") or "")
            target = str(binding.get("target") or "").strip()
            if not target:
                continue
            if kind == "program_summary":
                intake[target] = summary
            elif kind == "artifact":
                name = str(binding.get("name") or "").strip()
                if name and artifacts.get(name) is not None:
                    intake[target] = artifacts[name]
            elif kind == "static":
                intake[target] = copy.deepcopy(binding.get("value"))
            elif kind == "transcript_context":
                limit = int(binding.get("limit") or 20)
                intake[target] = {
                    "summary": summary,
                    "transcript": list(transcript_messages or [])[-limit:],
                }
    if source_workflow_id:
        intake["source_workflow_id"] = source_workflow_id
    return intake


def extract_outputs_for_node(
    *,
    snapshot: dict[str, Any],
    node_id: str,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    artifacts: dict[str, Any] | None = None,
    adapters: ProgramAdapterRegistry | None = None,
) -> dict[str, Any]:
    node = snapshot_node(snapshot, node_id)
    adapter_name = str(node.get("completion_adapter") or "").strip()
    if not adapter_name:
        return {}
    registry = adapters or build_default_adapter_registry()
    return registry.extract_outputs(
        adapter_name,
        workflow_type=workflow_type,
        tasks=tasks,
        artifacts=artifacts,
        required_artifacts=list(node.get("required_artifacts") or []),
        produces_artifacts=list(node.get("produces_artifacts") or []),
    )


def materialize_program_workflow(
    *,
    client: Any,
    skeleton: Any,
    workflow_type: str,
    program_id: str,
    program_phase: str,
    tenant_id: str,
    binding_key: str | None,
    requester_actor_id: str | None,
    intake_input: dict[str, Any],
    source_workflow_id: str | None = None,
    actor: str = "orchestrator",
    workflow_id: str | None = None,
) -> tuple[str, list[str], str]:
    import uuid

    workflow_id = workflow_id or str(uuid.uuid4())
    client.create_workflow(
        workflow_id=workflow_id,
        workflow_type=workflow_type,
        tenant_id=tenant_id,
        definition_version=skeleton.definition_version,
        source_channel=binding_key,
        requester_actor_id=requester_actor_id,
        correlation_id=f"program:{program_id}",
        parent_workflow_id=source_workflow_id,
        program_id=program_id,
        program_phase=program_phase,
        actor=actor,
    )
    client.link_workflow_to_program(
        program_id,
        workflow_id=workflow_id,
        program_phase=program_phase,
        actor=actor,
    )

    from ts_harness.jira.target_project import with_jira_target
    from ts_harness.workflows.delivery_program import _live_program_defaults

    intake = skeleton.intake_stage_name()
    stage_inputs = {intake: with_jira_target(dict(intake_input), _live_program_defaults())}
    task_bodies = materialize_skeleton_tasks(
        skeleton,
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        external_ref=binding_key,
        stage_inputs=stage_inputs,
    )

    name_to_id: dict[str, str] = {}
    task_ids: list[str] = []
    for body in task_bodies:
        depends_on_names = list(body.get("depends_on") or [])
        payload = {
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "tenant_id": tenant_id,
            "step_name": body["step_name"],
            "agent_id": body["agent_id"],
            "input": body.get("input") or {},
            "approval_required": body.get("approval_required", False),
            "max_retries": body.get("max_retries", 0),
            "external_ref": body.get("external_ref"),
            "contract": body.get("contract") or {},
            "definition_version": body.get("definition_version"),
            "lane": body.get("lane"),
            "actor": actor,
        }
        if depends_on_names:
            payload["depends_on"] = [
                name_to_id[name] for name in depends_on_names if name in name_to_id
            ]
        result = client.create_task(payload)
        task_id = str(result["id"])
        name_to_id[body["step_name"]] = task_id
        task_ids.append(task_id)

    return workflow_id, task_ids, intake


def program_pin_fields(
    program_type: str = "software_delivery",
    *,
    registry: Any | None = None,
) -> dict[str, Any]:
    """Build create_program pin kwargs for the active-or-built-in definition."""
    from ts_harness.workflows.program_registry import (
        get_program_definition_registry,
    )

    reg = registry or get_program_definition_registry()
    reg.reload_if_changed()
    snapshot = reg.load(program_type)
    return {
        "program_type": str(snapshot.get("program_type") or program_type),
        "program_definition_version": str(snapshot.get("definition_version") or ""),
        "program_definition_revision": reg.content_revision(program_type),
        "program_definition_snapshot": snapshot,
        "resolved_workflow_versions": build_resolved_workflow_versions(snapshot),
    }
