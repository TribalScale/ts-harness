"""Semantic validation for builder-authored workflow definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable

ensure_scripts_importable()

import workflow_definitions  # noqa: E402
import workflow_fragments  # noqa: E402


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    path: str
    message: str


def _issue(severity: str, code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,
        code=code,
        path=path,
        message=message,
    )


def _hook_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("hook") or "").strip()
    return ""


def _stage_path(index: int, suffix: str = "") -> str:
    return f"$.stages[{index}]{suffix}"


def _validate_hooks(
    payload: dict[str, Any],
    *,
    hook_names: set[str],
    issues: list[ValidationIssue],
) -> None:
    for field in ("on_workflow_cancel", "on_workflow_complete"):
        hook = _hook_name(payload.get(field))
        if hook and hook not in hook_names:
            issues.append(
                _issue(
                    "error",
                    "unknown_hook",
                    f"$.{field}.hook",
                    f"unknown hook: {hook}",
                )
            )

    for index, stage in enumerate(list(payload.get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        for field in ("on_complete", "on_reject", "on_block", "on_failure"):
            hook = _hook_name(stage.get(field))
            if hook and hook not in hook_names:
                issues.append(
                    _issue(
                        "error",
                        "unknown_hook",
                        _stage_path(index, f".{field}.hook"),
                        f"unknown hook: {hook}",
                    )
                )


def _validate_catalog_refs(
    payload: dict[str, Any],
    *,
    agent_ids: set[str],
    skill_ids: set[str],
    issues: list[ValidationIssue],
    agent_tools: dict[str, set[str]] | None = None,
) -> None:
    for index, stage in enumerate(list(payload.get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        agent_id = str(stage.get("agent_id") or "").strip()
        if agent_id and agent_id not in agent_ids:
            issues.append(
                _issue(
                    "error",
                    "unknown_agent",
                    _stage_path(index, ".agent_id"),
                    f"unknown agent: {agent_id}",
                )
            )
        allowed_agents = list(stage.get("allowed_agents") or [])
        for allowed_index, allowed_agent in enumerate(allowed_agents):
            allowed = str(allowed_agent or "").strip()
            if allowed and allowed not in agent_ids:
                issues.append(
                    _issue(
                        "error",
                        "unknown_agent",
                        _stage_path(index, f".allowed_agents[{allowed_index}]"),
                        f"unknown agent: {allowed}",
                    )
                )
        skill_hints = list(stage.get("skill_hints") or [])
        for skill_index, skill_id in enumerate(skill_hints):
            skill = str(skill_id or "").strip()
            if skill and skill not in skill_ids:
                issues.append(
                    _issue(
                        "error",
                        "unknown_skill",
                        _stage_path(index, f".skill_hints[{skill_index}]"),
                        f"unknown skill: {skill}",
                    )
                )
        tool_allow = stage.get("tool_allow")
        if agent_tools is None or not isinstance(tool_allow, list):
            continue
        granted = agent_tools.get(agent_id, set())
        for tool_index, tool_name in enumerate(tool_allow):
            tool = str(tool_name or "").strip()
            if tool and tool not in granted:
                issues.append(
                    _issue(
                        "error",
                        "unknown_tool",
                        _stage_path(index, f".tool_allow[{tool_index}]"),
                        f"tool not granted to {agent_id or 'stage agent'}: {tool}",
                    )
                )


def _validate_contract_refs(
    payload: dict[str, Any],
    *,
    issues: list[ValidationIssue],
) -> None:
    contracts = (
        payload.get("contracts")
        if isinstance(payload.get("contracts"), dict)
        else {}
    )
    contract_names = {str(name) for name in contracts}
    for index, stage in enumerate(list(payload.get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        contract_ref = str(stage.get("contract_ref") or "").strip()
        if contract_ref and contract_ref not in contract_names:
            issues.append(
                _issue(
                    "error",
                    "unresolved_contract_ref",
                    _stage_path(index, ".contract_ref"),
                    f"unresolved contract_ref: {contract_ref}",
                )
            )
        allowed_contracts = list(stage.get("allowed_contracts") or [])
        for ref_index, allowed_ref in enumerate(allowed_contracts):
            ref = str(allowed_ref or "").strip()
            if ref and ref not in contract_names:
                issues.append(
                    _issue(
                        "error",
                        "unresolved_contract_ref",
                        _stage_path(index, f".allowed_contracts[{ref_index}]"),
                        f"unresolved contract_ref: {ref}",
                    )
                )


_ALLOWED_INPUT_MAP_PREFIXES = (
    "$prior.",
    "$workflow.",
    "$reject.",
    "$input.",
    "$source.",
    "$runtime.",
    "$item.",
    "$revise.",
    "$re_gate.",
)


def _validate_input_maps(
    payload: dict[str, Any],
    *,
    issues: list[ValidationIssue],
) -> None:
    for index, stage in enumerate(list(payload.get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        input_map = stage.get("input_map") or {}
        if not isinstance(input_map, dict):
            continue
        for key, value in input_map.items():
            path = _stage_path(index, f".input_map.{key}")
            if not isinstance(value, str):
                issues.append(
                    _issue(
                        "error",
                        "unsupported_input_map",
                        path,
                        "input_map values must be strings",
                    )
                )
                continue
            token = value.strip()
            if token.startswith("$") and not token.startswith(
                _ALLOWED_INPUT_MAP_PREFIXES
            ):
                issues.append(
                    _issue(
                        "error",
                        "unsupported_input_map",
                        path,
                        f"unsupported input_map expression: {value}",
                    )
                )


def _stage_by_name(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    for stage in list(payload.get("stages") or []):
        if isinstance(stage, dict) and str(stage.get("name") or "").strip() == name:
            return stage
    return None


def _validate_hook_payloads(
    payload: dict[str, Any],
    *,
    issues: list[ValidationIssue],
) -> None:
    for index, stage in enumerate(list(payload.get("stages") or [])):
        if not isinstance(stage, dict):
            continue

        wait_external = stage.get("wait_external")
        if isinstance(wait_external, dict) and wait_external:
            event_type = str(wait_external.get("event_type") or "").strip()
            if not event_type:
                issues.append(
                    _issue(
                        "error",
                        "invalid_wait_external",
                        _stage_path(index, ".wait_external.event_type"),
                        "wait_external requires event_type",
                    )
                )

        for field in ("on_complete", "on_reject", "on_block", "on_failure"):
            config = stage.get(field)
            if not isinstance(config, dict):
                continue
            hook = _hook_name(config)
            if not hook:
                continue

            if hook == "hydrate_input":
                downstream = config.get("downstream")
                if not isinstance(downstream, list):
                    issues.append(
                        _issue(
                            "error",
                            "invalid_hydrate_input",
                            _stage_path(index, f".{field}.downstream"),
                            "hydrate_input requires downstream array",
                        )
                    )

            if hook == "compensate_on_failure":
                compensate_stage = str(config.get("compensate_stage") or "").strip()
                path = _stage_path(index, f".{field}.compensate_stage")
                if not compensate_stage:
                    issues.append(
                        _issue(
                            "error",
                            "invalid_compensate_on_failure",
                            path,
                            "compensate_on_failure requires compensate_stage",
                        )
                    )
                    continue
                target = _stage_by_name(payload, compensate_stage)
                if target is None or str(target.get("kind") or "") != "compensate":
                    issues.append(
                        _issue(
                            "error",
                            "invalid_compensate_on_failure",
                            path,
                            "compensate_stage must name an existing compensate stage",
                        )
                    )

            if hook == "revise_on_reject":
                revise = config.get("revise")
                step_name = ""
                if isinstance(revise, dict):
                    step_name = str(revise.get("step_name") or "").strip()
                if not step_name:
                    issues.append(
                        _issue(
                            "error",
                            "invalid_revise_on_reject",
                            _stage_path(index, f".{field}.revise.step_name"),
                            "revise_on_reject requires revise.step_name",
                        )
                    )
                if isinstance(revise, dict):
                    if "budget" in revise and not isinstance(revise.get("budget"), str):
                        issues.append(
                            _issue(
                                "error",
                                "invalid_revise_on_reject",
                                _stage_path(index, f".{field}.revise.budget"),
                                "revise.budget must be a string",
                            )
                        )
                    if "static_input" in revise and not isinstance(revise.get("static_input"), dict):
                        issues.append(
                            _issue(
                                "error",
                                "invalid_revise_on_reject",
                                _stage_path(index, f".{field}.revise.static_input"),
                                "revise.static_input must be an object",
                            )
                        )
                if "cases" in config:
                    _validate_block_cases(config.get("cases"), index=index, field=field, issues=issues)


def _validate_block_cases(cases: Any, *, index: int, field: str, issues: list[ValidationIssue]) -> None:
    def invalid(path: str, message: str) -> None:
        issues.append(_issue("error", "invalid_revise_on_reject", _stage_path(index, f".{field}.cases{path}"), message))

    if field != "on_block":
        invalid("", "cases are only supported on on_block")
        return
    if not isinstance(cases, list):
        invalid("", "cases must be an array")
        return
    for position, case in enumerate(cases):
        if not isinstance(case, dict):
            invalid(f"[{position}]", "each case must be an object")
            continue
        prefix = case.get("error_prefix")
        if not isinstance(prefix, str) or not prefix.strip():
            invalid(f"[{position}].error_prefix", "each case requires a non-empty error_prefix")
        if "action" in case and case.get("action") != "stop":
            invalid(f"[{position}].action", "case action must be \"stop\"")
        if "revise" in case:
            revise = case.get("revise")
            if not isinstance(revise, dict) or not str(revise.get("step_name") or "").strip():
                invalid(f"[{position}].revise.step_name", "case revise requires revise.step_name")
            elif "budget" in revise and not isinstance(revise.get("budget"), str):
                invalid(f"[{position}].revise.budget", "revise.budget must be a string")
        if "max_revisions" in case and (isinstance(case.get("max_revisions"), bool) or not isinstance(case.get("max_revisions"), int)):
            invalid(f"[{position}].max_revisions", "max_revisions must be an integer")


def _validate_graph(
    payload: dict[str, Any],
    *,
    issues: list[ValidationIssue],
) -> None:
    raw_stages = [
        stage
        for stage in list(payload.get("stages") or [])
        if isinstance(stage, dict)
    ]
    name_to_index: dict[str, int] = {}
    duplicate_names: set[str] = set()
    for index, stage in enumerate(raw_stages):
        name = str(stage.get("name") or "").strip()
        if not name:
            continue
        if name in name_to_index:
            duplicate_names.add(name)
            issues.append(
                _issue(
                    "error",
                    "duplicate_stage",
                    _stage_path(index, ".name"),
                    f"duplicate stage name: {name}",
                )
            )
        else:
            name_to_index[name] = index

    dependents: dict[str, set[str]] = {name: set() for name in name_to_index}
    deps_by_name: dict[str, list[str]] = {}
    for index, stage in enumerate(raw_stages):
        name = str(stage.get("name") or "").strip()
        if not name:
            continue
        deps = [str(dep) for dep in list(stage.get("depends_on") or [])]
        deps_by_name.setdefault(name, deps)
        for dep_index, dep in enumerate(deps):
            if dep == name:
                issues.append(
                    _issue(
                        "error",
                        "self_dependency",
                        _stage_path(index, f".depends_on[{dep_index}]"),
                        f"stage depends on itself: {name}",
                    )
                )
            elif dep not in name_to_index:
                issues.append(
                    _issue(
                        "error",
                        "missing_dependency",
                        _stage_path(index, f".depends_on[{dep_index}]"),
                        f"stage depends on missing stage: {dep}",
                    )
                )
            else:
                dependents.setdefault(dep, set()).add(name)

    roots = [name for name, deps in deps_by_name.items() if not deps]
    if raw_stages and not roots:
        issues.append(
            _issue(
                "error",
                "no_root_stage",
                "$.stages",
                "workflow has no root stage",
            )
        )

    visited: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in visited:
            continue
        visited.add(name)
        stack.extend(sorted(dependents.get(name, set()) - visited))
    for name, index in name_to_index.items():
        if roots and name not in visited:
            issues.append(
                _issue(
                    "error",
                    "unreachable_stage",
                    _stage_path(index),
                    f"stage is unreachable from root stages: {name}",
                )
            )

    visiting: set[str] = set()
    visited_cycles: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in visiting:
            issues.append(
                _issue(
                    "error",
                    "cycle",
                    "$.stages",
                    "dependency cycle detected: " + " -> ".join([*path, name]),
                )
            )
            return
        if name in visited_cycles:
            return
        visiting.add(name)
        for dep in deps_by_name.get(name, []):
            if dep in deps_by_name and dep != name:
                visit(dep, [*path, name])
        visiting.remove(name)
        visited_cycles.add(name)

    for name in sorted(deps_by_name):
        visit(name, [])

    if len(raw_stages) > 1:
        for name, index in name_to_index.items():
            if not deps_by_name.get(name) and not dependents.get(name):
                issues.append(
                    _issue(
                        "warning",
                        "isolated_stage",
                        _stage_path(index),
                        f"stage has no dependencies and no dependents: {name}",
                    )
                )

    has_approval = any(bool(stage.get("approval_required")) for stage in raw_stages)
    if raw_stages and not has_approval:
        issues.append(
            _issue(
                "warning",
                "no_approval_stage",
                "$.stages",
                "workflow has no approval_required stage",
            )
        )


PUBLISH_GATE_CODES = frozenset({"ungated_publish_stage", "unrestricted_mcp_before_gate"})


def _is_gate_stage(stage: dict[str, Any]) -> bool:
    if bool(stage.get("approval_required")):
        return True
    return str(stage.get("kind") or "") == "expand" and bool(stage.get("plan_approval_required"))


def _stage_agents(stage: dict[str, Any]) -> list[str]:
    agents = [str(stage.get("agent_id") or "").strip()]
    agents.extend(str(item or "").strip() for item in list(stage.get("allowed_agents") or []))
    return [agent for agent in dict.fromkeys(agents) if agent]


def _validate_publish_gates(
    payload: dict[str, Any],
    *,
    publish_tools: set[str],
    agent_tools: dict[str, set[str]] | None,
    issues: list[ValidationIssue],
) -> None:
    """Flag stages that can publish before any human approval gate.

    A stage's own ``approval_required`` approves its output after it runs, so only
    an upstream gate (through ``depends_on``) protects it.
    """
    stages = [stage for stage in list(payload.get("stages") or []) if isinstance(stage, dict)]
    by_name = {str(stage.get("name") or "").strip(): stage for stage in stages}
    gated_cache: dict[str, bool] = {}

    def has_gate_ancestor(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in gated_cache:
            return gated_cache[name]
        result = False
        for dep in list((by_name.get(name) or {}).get("depends_on") or []):
            dep_name = str(dep)
            parent = by_name.get(dep_name)
            if parent is None or dep_name in seen:
                continue
            if _is_gate_stage(parent) or has_gate_ancestor(dep_name, seen | {name}):
                result = True
                break
        gated_cache[name] = result
        return result

    for index, stage in enumerate(list(payload.get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        name = str(stage.get("name") or "").strip()
        if not name or has_gate_ancestor(name):
            continue
        tool_allow = stage.get("tool_allow")
        restricted = isinstance(tool_allow, list)
        if restricted:
            effective = {str(tool) for tool in tool_allow}
        elif agent_tools is not None:
            effective = set()
            for agent_id in _stage_agents(stage):
                effective |= set(agent_tools.get(agent_id, set()))
        else:
            continue
        reachable = sorted(effective & publish_tools)
        if reachable:
            issues.append(
                _issue(
                    "warning",
                    "ungated_publish_stage",
                    _stage_path(index, ".tool_allow" if restricted else ""),
                    f"stage {name} can call {', '.join(reachable)} before any approval gate; "
                    "add an approval_required stage before it or restrict its tools",
                )
            )
            continue
        if not restricted and any(tool.startswith("mcp-") for tool in effective):
            issues.append(
                _issue(
                    "warning",
                    "unrestricted_mcp_before_gate",
                    _stage_path(index),
                    f"stage {name} has unrestricted MCP tools before any approval gate; "
                    "restrict its tools",
                )
            )


_FORK_HOOKS = {"fork_children", "fork_work_order_wave"}
_CHILDREN_SOURCE_RE = re.compile(r"^\$prior\.([^.]+)\.output\.children$")
_WORKFLOW_TOKEN_PREFIX = "$workflow."
_IMPLICIT_WORKFLOW_KEYS = {"binding_key"}


def _fork_child_type(stage: dict[str, Any]) -> str:
    barrier = stage.get("barrier")
    if not isinstance(barrier, dict) or _hook_name(barrier) not in _FORK_HOOKS:
        return ""
    child_type = str(barrier.get("workflow_type") or "").strip()
    if not child_type and _hook_name(barrier) == "fork_work_order_wave":
        return "deliver_work_order"
    return child_type


def _reaches(graph: dict[str, set[str]], start: str, target: str) -> bool:
    seen: set[str] = set()
    pending = list(graph.get(start) or ())
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        pending.extend(graph.get(node) or ())
    return False


def _validate_forks(
    payload: dict[str, Any],
    *,
    workflow_catalog: dict[str, dict[str, Any]] | None,
    fork_graph: dict[str, set[str]] | None,
    issues: list[ValidationIssue],
) -> None:
    stages = [stage for stage in list(payload.get("stages") or []) if isinstance(stage, dict)]
    fork_stages: set[str] = set()
    children: set[str] = set()
    for index, stage in enumerate(stages):
        for field in ("on_complete", "on_reject", "on_block", "on_failure"):
            if _hook_name(stage.get(field)) == "fork_subworkflow":
                issues.append(
                    _issue(
                        "error",
                        "fork_subworkflow_retired",
                        _stage_path(index, f".{field}.hook"),
                        "fork_subworkflow is retired; use a barrier stage with hook fork_children and a join stage",
                    )
                )
        barrier = stage.get("barrier")
        if not isinstance(barrier, dict) or _hook_name(barrier) not in _FORK_HOOKS:
            continue
        fork_stages.add(str(stage.get("name") or ""))
        child_type = _fork_child_type(stage)
        if not child_type:
            issues.append(
                _issue("error", "fork_missing_workflow_type", _stage_path(index, ".barrier.workflow_type"), "fork_children requires workflow_type")
            )
            continue
        children.add(child_type)
        if workflow_catalog is None:
            continue
        entry = workflow_catalog.get(child_type)
        if entry is None:
            issues.append(
                _issue("error", "unknown_child_workflow", _stage_path(index, ".barrier.workflow_type"), f"unknown child workflow: {child_type}")
            )
            continue
        if not entry.get("resolvable"):
            issues.append(
                _issue(
                    "error",
                    "child_workflow_unresolvable",
                    _stage_path(index, ".barrier.workflow_type"),
                    f"child workflow cannot be loaded: {child_type}",
                )
            )
        if _hook_name(barrier) == "fork_children":
            mapped = set((barrier.get("input_map") or {}).keys())
            for key in sorted(set(entry.get("workflow_input_keys") or []) - mapped - _IMPLICIT_WORKFLOW_KEYS):
                issues.append(
                    _issue(
                        "warning",
                        "child_input_not_mapped",
                        _stage_path(index, ".barrier.input_map"),
                        f"child workflow {child_type} reads $workflow.{key} but input_map does not supply {key}",
                    )
                )

    workflow_type = str(payload.get("workflow_type") or "").strip()
    if workflow_type and children:
        graph = {key: set(value) for key, value in (fork_graph or {}).items()}
        graph[workflow_type] = children
        if _reaches(graph, workflow_type, workflow_type):
            issues.append(
                _issue("error", "child_workflow_cycle", "$.stages", f"child workflows fork back into {workflow_type}")
            )

    for index, stage in enumerate(stages):
        wait = stage.get("wait_external")
        spec = wait.get("children") if isinstance(wait, dict) else None
        if not isinstance(spec, dict):
            continue
        match = _CHILDREN_SOURCE_RE.match(str(spec.get("source") or "").strip())
        path = _stage_path(index, ".wait_external.children.source")
        if not match or match.group(1) not in fork_stages:
            issues.append(
                _issue("error", "join_source_not_fork", path, "children.source must be $prior.<fork stage>.output.children")
            )
        elif match.group(1) not in list(stage.get("depends_on") or []):
            issues.append(
                _issue("error", "join_missing_dependency", _stage_path(index, ".depends_on"), f"join must depend on fork stage {match.group(1)}")
            )


_STAGE_PATH_RE = re.compile(r"^\$\.stages\[(\d+)\](.*)$")


def _remap_issue(
    issue: ValidationIssue,
    expansion: workflow_fragments.Expansion,
    expanded_stages: list[Any],
) -> ValidationIssue:
    """Point an issue on an expanded stage back at the stage in the stored definition."""
    match = _STAGE_PATH_RE.match(issue.path)
    if not match or not expansion.includes:
        return issue
    position = int(match.group(1))
    if position >= len(expansion.index_map):
        return issue
    raw_index, include_name = expansion.index_map[position]
    if not include_name:
        return replace(issue, path=f"$.stages[{raw_index}]{match.group(2)}")
    include = next(item for item in expansion.includes if item["name"] == include_name)
    stage = expanded_stages[position]
    stage_name = str(stage.get("name") or "") if isinstance(stage, dict) else ""
    detail = f"{stage_name}{match.group(2)}" if stage_name else match.group(2)
    return replace(
        issue,
        path=f"$.stages[{raw_index}]",
        message=f"fragment {include['ref']} stage {detail}: {issue.message}",
    )


def _fragment_error_issue(exc: workflow_fragments.FragmentError) -> ValidationIssue:
    index = exc.include_index
    path = f"$.stages[{index}]" if index is not None else "$.stages"
    return _issue("error", "fragment_invalid", path, str(exc))


def validate_workflow_definition(
    payload: dict[str, Any],
    *,
    hook_names: set[str],
    agent_ids: set[str],
    skill_ids: set[str],
    built_in_types: set[str],
    allow_builtin_type: bool = False,
    workflow_catalog: dict[str, dict[str, Any]] | None = None,
    fork_graph: dict[str, set[str]] | None = None,
    agent_tools: dict[str, set[str]] | None = None,
    publish_tools: set[str] | None = None,
) -> tuple[Any | None, list[ValidationIssue]]:
    """``workflow_catalog`` and ``fork_graph`` enable child-workflow existence and cycle checks.

    ``agent_tools`` maps agent id to the tools a stage ``tool_allow`` may name.
    ``publish_tools`` enables the publish-gate warnings; ``None`` skips them.
    """
    try:
        expansion = workflow_fragments.expand_fragments(payload)
    except workflow_fragments.FragmentError as exc:
        return None, [_fragment_error_issue(exc)]
    expanded = expansion.payload
    skeleton, raw_issues = _validate_expanded(
        payload,
        expanded,
        hook_names=hook_names,
        agent_ids=agent_ids,
        skill_ids=skill_ids,
        built_in_types=built_in_types,
        allow_builtin_type=allow_builtin_type,
        agent_tools=agent_tools,
    )
    _validate_forks(expanded, workflow_catalog=workflow_catalog, fork_graph=fork_graph, issues=raw_issues)
    if publish_tools is not None:
        _validate_publish_gates(
            expanded,
            publish_tools=set(publish_tools),
            agent_tools=agent_tools,
            issues=raw_issues,
        )
    stages = list(expanded.get("stages") or [])
    return skeleton, [_remap_issue(issue, expansion, stages) for issue in raw_issues]


def _validate_expanded(
    payload: dict[str, Any],
    expanded: dict[str, Any],
    *,
    hook_names: set[str],
    agent_ids: set[str],
    skill_ids: set[str],
    built_in_types: set[str],
    allow_builtin_type: bool = False,
    agent_tools: dict[str, set[str]] | None = None,
) -> tuple[Any | None, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []

    skeleton = None
    try:
        skeleton = workflow_definitions.load_skeleton_payload(payload)
    except Exception as exc:  # noqa: BLE001
        issues.append(
            _issue(
                "error",
                "schema_load_error",
                "$",
                f"workflow definition failed to load: {exc}",
            )
        )
    payload = expanded

    workflow_type = str(payload.get("workflow_type") or "").strip()
    if workflow_type in built_in_types and not allow_builtin_type:
        issues.append(
            _issue(
                "error",
                "built_in_type_collision",
                "$.workflow_type",
                f"workflow_type collides with built-in workflow: {workflow_type}",
            )
        )

    _validate_hooks(payload, hook_names=hook_names, issues=issues)
    _validate_catalog_refs(
        payload,
        agent_ids=agent_ids,
        skill_ids=skill_ids,
        issues=issues,
        agent_tools=agent_tools,
    )
    _validate_contract_refs(payload, issues=issues)
    _validate_input_maps(payload, issues=issues)
    _validate_hook_payloads(payload, issues=issues)
    _validate_graph(payload, issues=issues)

    return skeleton, issues


_FRAGMENT_PLACEHOLDER_AGENT = "orchestrator"


def validate_fragment_definition(
    payload: dict[str, Any],
    *,
    hook_names: set[str],
    agent_ids: set[str],
    skill_ids: set[str],
) -> tuple[Any | None, list[ValidationIssue]]:
    """Validate a fragment by including it in a synthetic workflow.

    ``stage_ref`` params become placeholder stages and inputs are bound to
    ``$workflow.<name>``, so the regular workflow checks run on the fragment's
    stages. Issues are reported against ``$.stages[i]`` of the fragment.
    """
    try:
        fragment = workflow_fragments.load_fragment_payload(payload)
    except workflow_fragments.FragmentError as exc:
        return None, [_issue("error", "fragment_load_error", "$", str(exc))]

    placeholders = [
        {
            "name": f"param_{name}",
            "kind": "task",
            "agent_id": _FRAGMENT_PLACEHOLDER_AGENT,
            "approval_required": True,
        }
        for name in fragment.params
    ]
    include = {
        "name": f"include_{fragment.fragment_id}",
        "kind": "fragment",
        "fragment": fragment.fragment_id,
        "version": fragment.definition_version,
        "params": {name: f"param_{name}" for name in fragment.params},
        "inputs": {name: f"$workflow.{name}" for name in fragment.inputs},
        "depends_on": [stage["name"] for stage in placeholders],
    }
    synthetic = {
        "workflow_type": f"fragment_check_{fragment.fragment_id}",
        "definition_version": fragment.definition_version,
        "stages": [*placeholders, include],
    }
    issues: list[ValidationIssue] = []
    with workflow_fragments.override_resolver(lambda _id, _version: fragment):
        try:
            expansion = workflow_fragments.expand_fragments(synthetic)
        except workflow_fragments.FragmentError as exc:
            return None, [_issue("error", "fragment_invalid", "$", str(exc))]
        _skeleton, raw_issues = _validate_expanded(
            synthetic,
            expansion.payload,
            hook_names=hook_names,
            agent_ids=agent_ids | {_FRAGMENT_PLACEHOLDER_AGENT},
            skill_ids=skill_ids,
            built_in_types=set(),
        )
    offset = len(placeholders)
    for issue in raw_issues:
        match = _STAGE_PATH_RE.match(issue.path)
        if match:
            position = int(match.group(1))
            if position < offset:
                continue
            issues.append(replace(issue, path=f"$.stages[{position - offset}]{match.group(2)}"))
        elif issue.code not in {"no_approval_stage", "no_root_stage"}:
            issues.append(issue)

    params_used, inputs_used = workflow_fragments.referenced_names(fragment)
    for name in sorted(set(fragment.params) - params_used):
        issues.append(_issue("warning", "unused_fragment_param", f"$.params.{name}", f"param is never used: {name}"))
    for name in sorted(set(fragment.inputs) - inputs_used):
        issues.append(_issue("warning", "unused_fragment_input", f"$.inputs.{name}", f"input is never used: {name}"))
    return fragment, issues
