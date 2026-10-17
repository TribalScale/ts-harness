#!/usr/bin/env python3
# flake8: noqa
"""Program definition loading and canonicalization."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

try:  # jsonschema is a runtime dependency; guard for minimal environments.
    from jsonschema import Draft7Validator
except Exception:  # pragma: no cover - exercised only when dependency missing
    Draft7Validator = None  # type: ignore[assignment]


KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "program_type",
        "definition_version",
        "display_name",
        "startable_nodes",
        "nodes",
        "edges",
        "config",
        "required_permission",
    }
)
KNOWN_NODE_KEYS = frozenset(
    {
        "id",
        "default_workflow_type",
        "workflow_variants",
        "required_artifacts",
        "produces_artifacts",
        "input_bindings",
        "completion_adapter",
        "terminal",
        "reentry",
        "complete_channel_text",
        "await_children",
        "required_permission",
    }
)
KNOWN_EDGE_KEYS = frozenset(
    {
        "source",
        "target",
        "mode",
        "confirm_required",
        "signals",
        "artifact_patch",
    }
)
KNOWN_VARIANT_KEYS = frozenset({"workflow_type", "when"})
KNOWN_BINDING_KEYS = frozenset({"kind", "target", "name", "value", "limit"})
KNOWN_REENTRY_KEYS = frozenset(
    {
        "target_node",
        "next_item_artifact",
        "completed_list_artifact",
        "remaining_list_artifact",
        "mode",
        "max_iterations",
    }
)
REENTRY_MODES = frozenset({"list", "work_orders"})
MAX_REENTRY_ITERATIONS = 500
VALID_TRANSITION_MODES = frozenset({"manual", "automatic"})
VALID_BINDING_KINDS = frozenset(
    {
        "program_summary",
        "artifact",
        "static",
        "transcript_context",
    }
)
PERMISSION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_PROGRAM_PERMISSION = "start_program"

KNOWN_COMPLETION_ADAPTERS = frozenset(
    {
        "extract_prd_outputs",
        "extract_jira_outputs",
        "extract_work_orders_outputs",
        "extract_implement_outputs",
        "extract_deploy_outputs",
        "extract_declared_outputs",
        "extract_file_jira_outputs",
        "extract_pick_ticket_outputs",
    }
)


@dataclass(frozen=True)
class WorkflowVariant:
    workflow_type: str
    when: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgramNode:
    id: str
    default_workflow_type: str
    workflow_variants: list[WorkflowVariant] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    produces_artifacts: list[str] = field(default_factory=list)
    input_bindings: list[dict[str, Any]] = field(default_factory=list)
    completion_adapter: str = ""
    terminal: bool = False
    reentry: dict[str, Any] | None = None
    complete_channel_text: str | None = None
    await_children: bool = False
    required_permission: str = ""


@dataclass(frozen=True)
class ProgramEdge:
    source: str
    target: str
    mode: str
    confirm_required: bool = False
    signals: list[str] = field(default_factory=list)
    artifact_patch: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgramDefinition:
    program_type: str
    definition_version: str
    display_name: str
    startable_nodes: list[str]
    nodes: list[ProgramNode]
    edges: list[ProgramEdge]
    config: dict[str, Any] = field(default_factory=dict)
    required_permission: str = ""

    def node(self, node_id: str) -> ProgramNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(f"program node not found: {node_id}")


def required_permission_for(
    definition: ProgramDefinition | None,
    node_id: str | None = None,
) -> str:
    """Permission for entering ``node_id``, else the program, else ``start_program``."""
    if definition is None:
        return DEFAULT_PROGRAM_PERMISSION
    node_name = str(node_id or "").strip()
    if node_name:
        for node in definition.nodes:
            if node.id == node_name and node.required_permission:
                return node.required_permission
    if definition.required_permission:
        return definition.required_permission
    return DEFAULT_PROGRAM_PERMISSION


def _optional_permission(payload: dict[str, Any], key: str) -> str:
    if key not in payload or payload.get(key) is None:
        return ""
    value = payload.get(key)
    if not isinstance(value, str) or not PERMISSION_NAME_RE.match(value.strip()):
        raise ValueError(f"{key} must match {PERMISSION_NAME_RE.pattern}")
    return value.strip()


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return list(value)


def _reject_unknown_keys(
    payload: dict[str, Any],
    known_keys: frozenset[str],
    context: str,
) -> None:
    unknown = sorted(str(key) for key in payload if key not in known_keys)
    if unknown:
        raise ValueError(f"{context} contains unknown field(s): {', '.join(unknown)}")


def _validate_when(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} when must be an object")
    if not value:
        raise ValueError(f"{context} when must be a non-empty object")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{context} when keys must be non-empty strings")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError(
                f"{context} when values must be string, number, boolean, or null"
            )
        normalized[key.strip()] = item
    return normalized


def _load_variant(raw: dict[str, Any], context: str) -> WorkflowVariant:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _reject_unknown_keys(raw, KNOWN_VARIANT_KEYS, context)
    workflow_type = _require_string(raw, "workflow_type")
    if "when" not in raw:
        raise ValueError(f"{context} when is required")
    return WorkflowVariant(
        workflow_type=workflow_type,
        when=_validate_when(raw.get("when"), context),
    )


def _load_input_binding(raw: dict[str, Any], context: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _reject_unknown_keys(raw, KNOWN_BINDING_KEYS, context)
    kind = _require_string(raw, "kind")
    if kind not in VALID_BINDING_KINDS:
        raise ValueError(
            f"{context} kind must be one of: {', '.join(sorted(VALID_BINDING_KINDS))}"
        )
    target = _require_string(raw, "target")
    binding: dict[str, Any] = {"kind": kind, "target": target}
    if kind == "artifact":
        binding["name"] = _require_string(raw, "name")
    elif kind == "static":
        if "value" not in raw:
            raise ValueError(f"{context} value is required for static bindings")
        binding["value"] = copy.deepcopy(raw.get("value"))
    elif kind == "transcript_context":
        limit = raw.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"{context} limit must be a positive integer")
        binding["limit"] = limit
    elif "name" in raw or "value" in raw or "limit" in raw:
        raise ValueError(f"{context} includes fields unsupported for kind {kind}")
    return binding


def _load_reentry(raw: Any, context: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    _reject_unknown_keys(raw, KNOWN_REENTRY_KEYS, context)
    target_node = _require_string(raw, "target_node")
    remaining = str(raw.get("remaining_list_artifact") or "").strip()
    mode = str(raw.get("mode") or "").strip()
    if mode and mode not in REENTRY_MODES:
        raise ValueError(f"{context}.mode must be one of: {', '.join(sorted(REENTRY_MODES))}")
    if not mode:
        mode = "work_orders" if remaining in {"", "work_order_ids"} else "list"
    if mode == "list":
        if not remaining:
            raise ValueError(f"{context}.remaining_list_artifact is required in list mode")
        defaults = (remaining, f"completed_{remaining}", "next_item")
    else:
        defaults = ("work_order_ids", "completed_work_order_ids", "next_work_order_id")
    loaded: dict[str, Any] = {
        "target_node": target_node,
        "next_item_artifact": str(raw.get("next_item_artifact") or "").strip() or defaults[2],
        "completed_list_artifact": str(raw.get("completed_list_artifact") or "").strip() or defaults[1],
        "remaining_list_artifact": remaining or defaults[0],
    }
    names = [loaded["remaining_list_artifact"], loaded["completed_list_artifact"], loaded["next_item_artifact"]]
    if len(set(names)) != len(names):
        raise ValueError(f"{context} artifact names must be distinct: {', '.join(names)}")
    if raw.get("mode"):
        loaded["mode"] = mode
    if raw.get("max_iterations") is not None:
        limit = raw["max_iterations"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REENTRY_ITERATIONS:
            raise ValueError(f"{context}.max_iterations must be an integer from 1 to {MAX_REENTRY_ITERATIONS}")
        loaded["max_iterations"] = limit
    return loaded


def _load_node(raw: dict[str, Any]) -> ProgramNode:
    _reject_unknown_keys(raw, KNOWN_NODE_KEYS, "program node")
    node_id = _require_string(raw, "id")
    default_workflow_type = _require_string(raw, "default_workflow_type")
    raw_variants = raw.get("workflow_variants")
    if raw_variants is None:
        raw_variants = []
    if not isinstance(raw_variants, list):
        raise ValueError("workflow_variants must be an array")
    variants = [
        _load_variant(item, f"workflow_variants[{index}]")
        for index, item in enumerate(raw_variants)
    ]
    raw_bindings = raw.get("input_bindings")
    if raw_bindings is None:
        raw_bindings = []
    if not isinstance(raw_bindings, list):
        raise ValueError("input_bindings must be an array")
    bindings = [
        _load_input_binding(item, f"input_bindings[{index}]")
        for index, item in enumerate(raw_bindings)
    ]
    completion_adapter = _require_string(raw, "completion_adapter")
    if completion_adapter not in KNOWN_COMPLETION_ADAPTERS:
        raise ValueError(
            f"completion_adapter is not a known built-in adapter: {completion_adapter}"
        )
    terminal = raw.get("terminal")
    if not isinstance(terminal, bool):
        raise ValueError("terminal must be a boolean")
    complete_channel_text = raw.get("complete_channel_text")
    if complete_channel_text is not None and not isinstance(complete_channel_text, str):
        raise ValueError("complete_channel_text must be a string")
    await_children = raw.get("await_children", False)
    if not isinstance(await_children, bool):
        raise ValueError("await_children must be a boolean")
    return ProgramNode(
        id=node_id,
        default_workflow_type=default_workflow_type,
        workflow_variants=variants,
        required_artifacts=_string_list(raw.get("required_artifacts"), "required_artifacts"),
        produces_artifacts=_string_list(raw.get("produces_artifacts"), "produces_artifacts"),
        input_bindings=bindings,
        completion_adapter=completion_adapter,
        terminal=terminal,
        reentry=_load_reentry(raw.get("reentry"), "reentry"),
        complete_channel_text=complete_channel_text,
        await_children=await_children,
        required_permission=_optional_permission(raw, "required_permission"),
    )


def _load_edge(raw: dict[str, Any]) -> ProgramEdge:
    _reject_unknown_keys(raw, KNOWN_EDGE_KEYS, "program edge")
    source = _require_string(raw, "source")
    target = _require_string(raw, "target")
    mode = _require_string(raw, "mode")
    if mode not in VALID_TRANSITION_MODES:
        raise ValueError(
            f"unsupported transition mode: {mode}; "
            f"expected one of: {', '.join(sorted(VALID_TRANSITION_MODES))}"
        )
    confirm_required = raw.get("confirm_required", False)
    if not isinstance(confirm_required, bool):
        raise ValueError("confirm_required must be a boolean")
    artifact_patch = raw.get("artifact_patch")
    if artifact_patch is None:
        artifact_patch = {}
    if not isinstance(artifact_patch, dict):
        raise ValueError("artifact_patch must be an object")
    for key, value in artifact_patch.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("artifact_patch keys must be non-empty strings")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                "artifact_patch values must be string, number, boolean, or null"
            )
    return ProgramEdge(
        source=source,
        target=target,
        mode=mode,
        confirm_required=confirm_required,
        signals=_string_list(raw.get("signals"), "signals"),
        artifact_patch=dict(artifact_patch),
    )


def load_program_payload(payload: dict[str, Any]) -> ProgramDefinition:
    if not isinstance(payload, dict):
        raise ValueError("program definition must be an object")
    data = dict(payload)
    _reject_unknown_keys(data, KNOWN_TOP_LEVEL_KEYS, "program definition")
    program_type = _require_string(data, "program_type")
    definition_version = _require_string(data, "definition_version")
    display_name = _require_string(data, "display_name")
    startable_nodes = _string_list(data.get("startable_nodes"), "startable_nodes")
    if not startable_nodes:
        raise ValueError("startable_nodes must be a non-empty array")

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("nodes must be a non-empty array")
    nodes = [_load_node(node) for node in raw_nodes]
    node_ids = [node.id for node in nodes]
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("node ids must be unique")

    raw_edges = data.get("edges")
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise ValueError("edges must be an array")
    edges = [_load_edge(edge) for edge in raw_edges]

    node_id_set = set(node_ids)
    for node_id in startable_nodes:
        if node_id not in node_id_set:
            raise ValueError(f"startable_nodes references unknown node: {node_id}")
    for edge in edges:
        if edge.source not in node_id_set:
            raise ValueError(f"edge source references unknown node: {edge.source}")
        if edge.target not in node_id_set:
            raise ValueError(f"edge target references unknown node: {edge.target}")

    if Draft7Validator is not None:
        schema_path = Path(__file__).resolve().parents[1] / "config" / "programs" / "schema.json"
        if schema_path.is_file():
            schema = json.loads(schema_path.read_text())
            validator = Draft7Validator(schema)
            errors = sorted(validator.iter_errors(payload), key=lambda err: list(err.path))
            if errors:
                first = errors[0]
                path = "/".join(str(part) for part in first.path)
                location = f" at {path}" if path else ""
                raise ValueError(f"program definition schema invalid{location}: {first.message}")

    config = data.get("config")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("config must be an object")

    return ProgramDefinition(
        program_type=program_type,
        definition_version=definition_version,
        display_name=display_name,
        startable_nodes=startable_nodes,
        nodes=nodes,
        edges=edges,
        config=copy.deepcopy(config),
        required_permission=_optional_permission(data, "required_permission"),
    )


def load_program(path: str | Path) -> ProgramDefinition:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("program definition must be an object")
    return load_program_payload(data)


def _variant_to_payload(variant: WorkflowVariant) -> dict[str, Any]:
    return {
        "workflow_type": variant.workflow_type,
        "when": copy.deepcopy(variant.when),
    }


def _node_to_payload(node: ProgramNode) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": node.id,
        "default_workflow_type": node.default_workflow_type,
        "workflow_variants": [_variant_to_payload(variant) for variant in node.workflow_variants],
        "required_artifacts": list(node.required_artifacts),
        "produces_artifacts": list(node.produces_artifacts),
        "input_bindings": [copy.deepcopy(binding) for binding in node.input_bindings],
        "completion_adapter": node.completion_adapter,
        "terminal": node.terminal,
    }
    if node.reentry is not None:
        payload["reentry"] = copy.deepcopy(node.reentry)
    if node.complete_channel_text is not None:
        payload["complete_channel_text"] = node.complete_channel_text
    if node.await_children:
        payload["await_children"] = True
    if node.required_permission:
        payload["required_permission"] = node.required_permission
    return payload


def _edge_to_payload(edge: ProgramEdge) -> dict[str, Any]:
    return {
        "source": edge.source,
        "target": edge.target,
        "mode": edge.mode,
        "confirm_required": edge.confirm_required,
        "signals": list(edge.signals),
        "artifact_patch": copy.deepcopy(edge.artifact_patch),
    }


def program_to_payload(definition: ProgramDefinition) -> dict[str, Any]:
    payload = {
        "program_type": definition.program_type,
        "definition_version": definition.definition_version,
        "display_name": definition.display_name,
        "startable_nodes": list(definition.startable_nodes),
        "nodes": [_node_to_payload(node) for node in definition.nodes],
        "edges": [_edge_to_payload(edge) for edge in definition.edges],
    }
    if definition.config:
        payload["config"] = copy.deepcopy(definition.config)
    if definition.required_permission:
        payload["required_permission"] = definition.required_permission
    return payload


def canonical_program_json(payload: dict[str, Any]) -> str:
    definition = load_program_payload(payload)
    canonical_payload = program_to_payload(definition)
    return json.dumps(canonical_payload, indent=2, sort_keys=True) + "\n"
