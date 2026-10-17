"""Semantic validation for builder-authored program definitions."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.definition_validation import ValidationIssue
from ts_harness.workflows.program_adapters import ADAPTER_DECLARED_OUTPUT_KEYS

ensure_scripts_importable()

import program_definitions  # noqa: E402


_PRIMITIVE_TYPES = (str, int, float, bool)


def _issue(severity: str, code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,
        code=code,
        path=path,
        message=message,
    )


def _node_path(index: int, suffix: str = "") -> str:
    return f"$.nodes[{index}]{suffix}"


def _edge_path(index: int, suffix: str = "") -> str:
    return f"$.edges[{index}]{suffix}"


def _catalog_entry(
    workflow_catalog: dict[str, Any],
    workflow_type: str,
) -> dict[str, Any] | None:
    entry = workflow_catalog.get(workflow_type)
    if isinstance(entry, dict):
        return entry
    return None


def _contract_keys(entry: dict[str, Any] | None) -> set[str]:
    if entry is None:
        return set()
    raw = entry.get("contract_output_keys") or []
    return {str(item) for item in raw}


def _is_primitive(value: Any) -> bool:
    return value is None or isinstance(value, _PRIMITIVE_TYPES)


def _canonical_when(when: dict[str, Any]) -> str:
    return json.dumps(when, sort_keys=True, separators=(",", ":"))


def _declared_artifacts(definition: program_definitions.ProgramDefinition) -> set[str]:
    names: set[str] = set()
    for node in definition.nodes:
        names.update(node.required_artifacts)
        names.update(node.produces_artifacts)
    for edge in definition.edges:
        names.update(str(key) for key in edge.artifact_patch)
    return names


def _validate_identity(
    payload: dict[str, Any],
    *,
    built_in_types: set[str],
    allow_builtin_type: bool,
    issues: list[ValidationIssue],
) -> None:
    program_type = str(payload.get("program_type") or "").strip()
    if program_type in built_in_types and not allow_builtin_type:
        issues.append(
            _issue(
                "error",
                "built_in_type_collision",
                "$.program_type",
                f"program_type collides with built-in program: {program_type}",
            )
        )


def _validate_adapters(
    payload: dict[str, Any],
    *,
    adapter_names: set[str],
    issues: list[ValidationIssue],
) -> None:
    for index, node in enumerate(list(payload.get("nodes") or [])):
        if not isinstance(node, dict):
            continue
        adapter = str(node.get("completion_adapter") or "").strip()
        if adapter and adapter not in adapter_names:
            issues.append(
                _issue(
                    "error",
                    "unknown_completion_adapter",
                    _node_path(index, ".completion_adapter"),
                    f"unknown completion adapter: {adapter}",
                )
            )


def _validate_bindings_and_patches(
    payload: dict[str, Any],
    *,
    issues: list[ValidationIssue],
) -> None:
    for index, node in enumerate(list(payload.get("nodes") or [])):
        if not isinstance(node, dict):
            continue
        bindings = node.get("input_bindings") or []
        if not isinstance(bindings, list):
            continue
        for binding_index, binding in enumerate(bindings):
            if not isinstance(binding, dict):
                continue
            kind = str(binding.get("kind") or "").strip()
            path = _node_path(index, f".input_bindings[{binding_index}]")
            if kind and kind not in program_definitions.VALID_BINDING_KINDS:
                issues.append(
                    _issue(
                        "error",
                        "unsupported_input_binding",
                        path,
                        f"unsupported input binding kind: {kind}",
                    )
                )
                continue
            if kind == "static" and not _is_primitive(binding.get("value")):
                issues.append(
                    _issue(
                        "error",
                        "unsupported_input_binding",
                        f"{path}.value",
                        "static binding values must be string, number, boolean, or null",
                    )
                )

    for index, edge in enumerate(list(payload.get("edges") or [])):
        if not isinstance(edge, dict):
            continue
        patch = edge.get("artifact_patch") or {}
        if not isinstance(patch, dict):
            issues.append(
                _issue(
                    "error",
                    "unsupported_artifact_patch",
                    _edge_path(index, ".artifact_patch"),
                    "artifact_patch must be an object",
                )
            )
            continue
        for key, value in patch.items():
            if not isinstance(key, str) or not key.strip():
                issues.append(
                    _issue(
                        "error",
                        "unsupported_artifact_patch",
                        _edge_path(index, ".artifact_patch"),
                        "artifact_patch keys must be non-empty strings",
                    )
                )
            if not _is_primitive(value):
                issues.append(
                    _issue(
                        "error",
                        "unsupported_artifact_patch",
                        _edge_path(index, f".artifact_patch.{key}"),
                        "artifact_patch values must be string, number, boolean, or null",
                    )
                )


def _validate_workflows(
    definition: program_definitions.ProgramDefinition,
    *,
    workflow_catalog: dict[str, Any],
    issues: list[ValidationIssue],
) -> None:
    node_index = {node.id: index for index, node in enumerate(definition.nodes)}

    def check_ref(workflow_type: str, path: str) -> None:
        entry = _catalog_entry(workflow_catalog, workflow_type)
        if entry is None:
            issues.append(
                _issue(
                    "error",
                    "unresolved_workflow",
                    path,
                    f"workflow type is not in catalog: {workflow_type}",
                )
            )
            return
        if not bool(entry.get("resolvable")):
            issues.append(
                _issue(
                    "error",
                    "unresolvable_workflow",
                    path,
                    f"workflow type is not resolvable: {workflow_type}",
                )
            )
            return
        versions = [
            str(item)
            for item in list(entry.get("available_versions") or [])
            if str(item).strip()
        ]
        active_version = str(entry.get("active_version") or "").strip()
        if active_version and versions and active_version not in versions:
            issues.append(
                _issue(
                    "error",
                    "unresolved_workflow_version",
                    path,
                    (
                        f"pinned workflow version is unavailable: "
                        f"{workflow_type}@{active_version}"
                    ),
                )
            )

    for node in definition.nodes:
        index = node_index[node.id]
        check_ref(
            node.default_workflow_type,
            _node_path(index, ".default_workflow_type"),
        )
        for variant_index, variant in enumerate(node.workflow_variants):
            check_ref(
                variant.workflow_type,
                _node_path(index, f".workflow_variants[{variant_index}].workflow_type"),
            )


def _validate_variants(
    definition: program_definitions.ProgramDefinition,
    *,
    issues: list[ValidationIssue],
) -> None:
    declared = _declared_artifacts(definition)
    node_index = {node.id: index for index, node in enumerate(definition.nodes)}
    for node in definition.nodes:
        index = node_index[node.id]
        seen: dict[str, int] = {}
        canonical_order = [
            _canonical_when(dict(variant.when))
            for variant in node.workflow_variants
        ]
        if canonical_order != sorted(canonical_order):
            issues.append(
                _issue(
                    "error",
                    "nondeterministic_variant_order",
                    _node_path(index, ".workflow_variants"),
                    f"workflow_variants must be deterministically ordered: {node.id}",
                )
            )
        for variant_index, variant in enumerate(node.workflow_variants):
            key = _canonical_when(dict(variant.when))
            if key in seen:
                issues.append(
                    _issue(
                        "error",
                        "duplicate_variant_condition",
                        _node_path(
                            index,
                            f".workflow_variants[{variant_index}].when",
                        ),
                        f"duplicate variant condition on node {node.id}",
                    )
                )
            else:
                seen[key] = variant_index
            for artifact_name in variant.when:
                if str(artifact_name) not in declared:
                    issues.append(
                        _issue(
                            "error",
                            "unknown_variant_condition_artifact",
                            _node_path(
                                index,
                                f".workflow_variants[{variant_index}].when.{artifact_name}",
                            ),
                            (
                                "variant condition references undeclared artifact: "
                                f"{artifact_name}"
                            ),
                        )
                    )


def _validate_graph(
    definition: program_definitions.ProgramDefinition,
    *,
    issues: list[ValidationIssue],
) -> None:
    node_ids = [node.id for node in definition.nodes]
    node_index = {node.id: index for index, node in enumerate(definition.nodes)}
    seen_ids: set[str] = set()
    for node in definition.nodes:
        if node.id in seen_ids:
            issues.append(
                _issue(
                    "error",
                    "duplicate_node",
                    _node_path(node_index[node.id], ".id"),
                    f"duplicate node id: {node.id}",
                )
            )
        seen_ids.add(node.id)

    startable = list(definition.startable_nodes)
    if len(set(startable)) != len(startable):
        issues.append(
            _issue(
                "error",
                "duplicate_startable_node",
                "$.startable_nodes",
                "startable_nodes contains duplicates",
            )
        )
    for start_index, node_id in enumerate(startable):
        if node_id not in node_index:
            issues.append(
                _issue(
                    "error",
                    "unknown_startable_node",
                    f"$.startable_nodes[{start_index}]",
                    f"startable_nodes references unknown node: {node_id}",
                )
            )

    outgoing: dict[str, list[int]] = defaultdict(list)
    incoming: dict[str, list[int]] = defaultdict(list)
    for edge_index, edge in enumerate(definition.edges):
        if edge.source not in node_index:
            issues.append(
                _issue(
                    "error",
                    "unknown_edge_endpoint",
                    _edge_path(edge_index, ".source"),
                    f"edge source references unknown node: {edge.source}",
                )
            )
        if edge.target not in node_index:
            issues.append(
                _issue(
                    "error",
                    "unknown_edge_endpoint",
                    _edge_path(edge_index, ".target"),
                    f"edge target references unknown node: {edge.target}",
                )
            )
        if edge.source == edge.target:
            issues.append(
                _issue(
                    "error",
                    "self_edge",
                    _edge_path(edge_index),
                    f"self-edge is not allowed: {edge.source}",
                )
            )
        if edge.source in node_index:
            outgoing[edge.source].append(edge_index)
        if edge.target in node_index:
            incoming[edge.target].append(edge_index)

    # Reachability from startable nodes.
    reachable: set[str] = set()
    stack = [node_id for node_id in startable if node_id in node_index]
    while stack:
        node_id = stack.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        for edge_index in outgoing.get(node_id, []):
            target = definition.edges[edge_index].target
            if target in node_index and target not in reachable:
                stack.append(target)

    for node_id, index in node_index.items():
        if node_id not in startable and node_id not in reachable:
            issues.append(
                _issue(
                    "error",
                    "unreachable_node",
                    _node_path(index),
                    f"node is unreachable from startable nodes: {node_id}",
                )
            )

    for node in definition.nodes:
        index = node_index[node.id]
        outs = outgoing.get(node.id, [])
        if node.terminal and outs:
            issues.append(
                _issue(
                    "error",
                    "terminal_has_outgoing_edge",
                    _node_path(index),
                    f"terminal node must not have outgoing edges: {node.id}",
                )
            )
        if not node.terminal and not outs:
            issues.append(
                _issue(
                    "error",
                    "missing_outgoing_edge",
                    _node_path(index),
                    f"nonterminal node requires an outgoing edge: {node.id}",
                )
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str, path: list[str]) -> None:
        if node_id in visiting:
            issues.append(
                _issue(
                    "error",
                    "cycle",
                    "$.edges",
                    "edge cycle detected: " + " -> ".join([*path, node_id]),
                )
            )
            return
        if node_id in visited:
            return
        visiting.add(node_id)
        for edge_index in outgoing.get(node_id, []):
            target = definition.edges[edge_index].target
            if target in node_index and target != node_id:
                visit(target, [*path, node_id])
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in node_ids:
        visit(node_id, [])


def _ancestors(
    definition: program_definitions.ProgramDefinition,
    node_id: str,
) -> set[str]:
    incoming: dict[str, set[str]] = defaultdict(set)
    for edge in definition.edges:
        incoming[edge.target].add(edge.source)
    seen: set[str] = set()
    stack = list(incoming.get(node_id, set()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(incoming.get(current, set()) - seen)
    return seen


def _validate_artifacts(
    definition: program_definitions.ProgramDefinition,
    *,
    workflow_catalog: dict[str, Any],
    issues: list[ValidationIssue],
) -> None:
    startable = set(definition.startable_nodes)
    produces: dict[str, set[str]] = {
        node.id: set(node.produces_artifacts) for node in definition.nodes
    }
    edge_patches: dict[str, set[str]] = defaultdict(set)
    for edge in definition.edges:
        edge_patches[edge.target].update(str(key) for key in edge.artifact_patch)

    node_index = {node.id: index for index, node in enumerate(definition.nodes)}
    for node in definition.nodes:
        index = node_index[node.id]
        available: set[str] = set()
        if node.id in startable:
            # Entry points may receive required artifacts as external inputs.
            available.update(node.required_artifacts)
        for ancestor in _ancestors(definition, node.id):
            available.update(produces.get(ancestor, set()))
            available.update(edge_patches.get(node.id, set()))
            # Patches on edges into ancestors also contribute along the path.
            available.update(edge_patches.get(ancestor, set()))
        # Direct inbound patches onto this node.
        available.update(edge_patches.get(node.id, set()))
        if node.reentry and node.reentry.get("next_item_artifact"):
            # Seeded from the remaining list on entry and on every reentry.
            available.add(str(node.reentry["next_item_artifact"]))

        for artifact in node.required_artifacts:
            if artifact in available:
                continue
            if node.id in startable:
                continue
            issues.append(
                _issue(
                    "error",
                    "missing_required_artifact",
                    _node_path(index, ".required_artifacts"),
                    (
                        f"required artifact {artifact!r} is not available from "
                        f"an entry point or upstream node for {node.id}"
                    ),
                )
            )

        workflow_types = [node.default_workflow_type] + [
            variant.workflow_type for variant in node.workflow_variants
        ]
        confirmed: set[str] = set()
        for workflow_type in workflow_types:
            confirmed.update(
                _contract_keys(_catalog_entry(workflow_catalog, workflow_type))
            )
        adapter_id = str(node.completion_adapter or "").strip()
        adapter_keys = ADAPTER_DECLARED_OUTPUT_KEYS.get(adapter_id, frozenset())
        if adapter_keys is None:
            # Generic adapter: node produces_artifacts is the declaration.
            confirmed.update(node.produces_artifacts)
        else:
            confirmed.update(adapter_keys)
        for artifact in node.produces_artifacts:
            if artifact in confirmed:
                continue
            issues.append(
                _issue(
                    "warning",
                    "unconfirmed_output_artifact",
                    _node_path(index, ".produces_artifacts"),
                    (
                        f"workflow contract / completion adapter cannot statically "
                        f"confirm produced artifact {artifact!r} on node {node.id}"
                    ),
                )
            )


def _validate_reentry(
    definition: program_definitions.ProgramDefinition,
    *,
    issues: list[ValidationIssue],
) -> None:
    for index, node in enumerate(definition.nodes):
        reentry = node.reentry
        if not reentry:
            continue
        path = _node_path(index, ".reentry")
        if str(reentry.get("target_node") or "") != node.id:
            issues.append(
                _issue(
                    "error",
                    "reentry_not_self",
                    path,
                    f"reentry.target_node must be the declaring node {node.id!r}",
                )
            )
        remaining = str(reentry.get("remaining_list_artifact") or "")
        completed = str(reentry.get("completed_list_artifact") or "")
        mode = str(reentry.get("mode") or "")
        if not mode:
            mode = "work_orders" if remaining == "work_order_ids" else "list"
        if mode != "list":
            continue
        own_lists = sorted({remaining, completed} & set(node.produces_artifacts))
        if own_lists:
            issues.append(
                _issue(
                    "error",
                    "reentry_produces_own_list",
                    path,
                    (
                        f"loop node {node.id} must not produce its own list artifacts "
                        f"({', '.join(own_lists)}); they would be overwritten each pass"
                    ),
                )
            )
        next_item = str(reentry.get("next_item_artifact") or "")
        if next_item not in node.required_artifacts:
            issues.append(
                _issue(
                    "warning",
                    "reentry_item_not_required",
                    path,
                    (
                        f"next_item_artifact {next_item!r} is not in required_artifacts of {node.id}; "
                        "the declared adapter only passes required artifacts to the workflow"
                    ),
                )
            )


def _validate_required_permissions(
    definition: program_definitions.ProgramDefinition,
    *,
    config: Any,
    issues: list[ValidationIssue],
) -> None:
    from ts_harness.policy.permissions import known_permission_names

    known = known_permission_names(config)
    names = [(definition.required_permission, "$.required_permission")]
    names.extend(
        (node.required_permission, _node_path(index, ".required_permission"))
        for index, node in enumerate(definition.nodes)
    )
    for name, path in names:
        if name and name not in known:
            issues.append(
                _issue(
                    "error",
                    "unknown_permission",
                    path,
                    f"required_permission {name!r} is not in the permission catalog or a role bundle",
                )
            )


def validate_program_definition(
    payload: dict[str, Any],
    *,
    workflow_catalog: dict[str, Any],
    adapter_names: set[str],
    built_in_types: set[str],
    allow_builtin_type: bool = False,
    config: Any = None,
) -> tuple[Any | None, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    raw_payload = payload if isinstance(payload, dict) else {}

    definition = None
    try:
        definition = program_definitions.load_program_payload(raw_payload)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            _issue(
                "error",
                "schema_load_error",
                "$",
                f"program definition failed to load: {exc}",
            )
        )

    _validate_identity(
        raw_payload,
        built_in_types=built_in_types,
        allow_builtin_type=allow_builtin_type,
        issues=issues,
    )
    _validate_adapters(
        raw_payload,
        adapter_names=adapter_names,
        issues=issues,
    )
    from ts_harness.jira.program_config import validate_program_config

    for message in validate_program_config(raw_payload.get("config")):
        issues.append(_issue("error", "invalid_program_config", "$.config", message))
    _validate_bindings_and_patches(raw_payload, issues=issues)

    if definition is not None:
        _validate_graph(definition, issues=issues)
        _validate_workflows(
            definition,
            workflow_catalog=workflow_catalog,
            issues=issues,
        )
        _validate_variants(definition, issues=issues)
        _validate_artifacts(
            definition,
            workflow_catalog=workflow_catalog,
            issues=issues,
        )
        _validate_reentry(definition, issues=issues)
        _validate_required_permissions(definition, config=config, issues=issues)

    return definition, issues
