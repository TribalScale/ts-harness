#!/usr/bin/env python3
# flake8: noqa
"""Workflow skeleton loading and policy validation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import sys
from typing import Any
import warnings

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import workflow_fragments  # noqa: E402

try:  # jsonschema is a runtime dependency; guard for minimal environments.
    from jsonschema import Draft7Validator
except Exception:  # pragma: no cover - exercised only when dependency missing
    Draft7Validator = None  # type: ignore[assignment]


DEFAULT_MAX_RETRIES = 2
DEFAULT_TIMEOUT_SECONDS = 1800
VALID_STAGE_KINDS = {"task", "expand", "compensate"}
KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "workflow_type",
        "definition_version",
        "display_name",
        "promotion",
        "notifications",
        "on_workflow_cancel",
        "session",
        "bridge",
        "program",
        "deprecated",
        "superseded_by",
        "contracts",
        "stages",
        "experimental",
        "on_workflow_complete",
    }
)
KNOWN_STAGE_KEYS = frozenset(
    {
        "name",
        "kind",
        "agent_id",
        "allowed_agents",
        "allowed_external_effects",
        "depends_on",
        "required_artifacts",
        "skill_hints",
        "tool_allow",
        "retry",
        "approval_required",
        "approval",
        "plan_approval_required",
        "input_map",
        "on_reject",
        "on_block",
        "on_failure",
        "on_complete",
        "wait_external",
        "join",
        "barrier",
        "present_output",
        "present_prompt",
        "contract_ref",
        "allowed_contracts",
        "output_schema",
    }
)

# Semantic validators that stage contracts may reference. These names must match
# the registry keys in runtime/ts_harness/workflows/stage_contracts.py.
KNOWN_SEMANTIC_VALIDATORS = frozenset(
    {
        "validate_coding_backend_pr_url",
        "validate_work_order_artifacts",
        "validate_qa_pr_url_set",
        "validate_code_review_envelope",
    }
)


@dataclass(frozen=True)
class Stage:
    name: str
    kind: str
    agent_id: str
    allowed_agents: list[str]
    allowed_external_effects: list[str]
    depends_on: list[str]
    required_artifacts: list[str]
    skill_hints: list[str]
    retry: dict[str, int]
    approval_required: bool = False
    plan_approval_required: bool = False
    input_map: dict[str, str] = field(default_factory=dict)
    on_reject: dict[str, Any] = field(default_factory=dict)
    on_block: dict[str, Any] = field(default_factory=dict)
    on_failure: dict[str, Any] = field(default_factory=dict)
    on_complete: dict[str, Any] = field(default_factory=dict)
    wait_external: dict[str, Any] = field(default_factory=dict)
    join: dict[str, Any] = field(default_factory=dict)
    barrier: dict[str, Any] = field(default_factory=dict)
    present_output: bool | None = None
    present_prompt: str = ""
    contract_ref: str = ""
    allowed_contracts: list[str] = field(default_factory=list)
    output_schema: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    # None inherits the agent's tools; a list is the stage's whole tool set.
    tool_allow: list[str] | None = None
    # Name of the fragment include this stage was expanded from, if any.
    origin: str = ""


APPROVAL_POLICY_KEYS = frozenset(
    {"required_roles", "expires_after_seconds", "escalate_to_role", "decision_kind", "require_dependencies_succeeded"}
)
APPROVAL_DECISION_KINDS = frozenset({"gate", "promote"})


def _load_approval_policy(raw: Any, *, stage_name: str, approval_required: bool) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"stage '{stage_name}' approval must be an object")
    _reject_unknown_keys(raw, APPROVAL_POLICY_KEYS, f"stage '{stage_name}' approval")
    if not approval_required:
        raise ValueError(f"stage '{stage_name}' approval requires approval_required: true")
    policy: dict[str, Any] = {}
    roles = _string_list(raw.get("required_roles"), "approval.required_roles")
    if roles:
        policy["required_roles"] = sorted({role.strip().lower() for role in roles if role.strip()})
    if raw.get("expires_after_seconds") is not None:
        seconds = raw.get("expires_after_seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise ValueError(
                f"stage '{stage_name}' approval.expires_after_seconds must be a positive integer"
            )
        policy["expires_after_seconds"] = seconds
    escalate = str(raw.get("escalate_to_role") or "").strip().lower()
    if escalate:
        policy["escalate_to_role"] = escalate
    if raw.get("decision_kind") is not None:
        kind = str(raw.get("decision_kind") or "").strip().lower()
        if kind not in APPROVAL_DECISION_KINDS:
            raise ValueError(
                f"stage '{stage_name}' approval.decision_kind must be one of: {', '.join(sorted(APPROVAL_DECISION_KINDS))}"
            )
        policy["decision_kind"] = kind
    if raw.get("require_dependencies_succeeded") is not None:
        if not isinstance(raw.get("require_dependencies_succeeded"), bool):
            raise ValueError(f"stage '{stage_name}' approval.require_dependencies_succeeded must be a boolean")
        policy["require_dependencies_succeeded"] = raw["require_dependencies_succeeded"]
    return policy


@dataclass(frozen=True)
class WorkflowSkeleton:
    workflow_type: str
    definition_version: str
    stages: list[Stage]
    display_name: str = ""
    promotion: dict[str, Any] = field(default_factory=dict)
    notifications: dict[str, Any] = field(default_factory=dict)
    on_workflow_cancel: dict[str, Any] = field(default_factory=dict)
    session: dict[str, Any] = field(default_factory=dict)
    bridge: dict[str, Any] = field(default_factory=dict)
    program: dict[str, Any] = field(default_factory=dict)
    deprecated: bool = False
    superseded_by: str = ""
    contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    experimental: bool = False
    on_workflow_complete: dict[str, Any] = field(default_factory=dict)
    includes: list[dict[str, Any]] = field(default_factory=list)

    def stage(self, name: str) -> Stage:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(f"workflow stage not found: {name}")

    def intake_stage_name(self) -> str:
        intake = str((self.promotion or {}).get("intake_stage") or "").strip()
        if intake:
            return intake
        return self.stages[0].name

    def contract(self, name: str) -> dict[str, Any]:
        if name not in self.contracts:
            raise KeyError(f"workflow contract not found: {name}")
        return copy.deepcopy(self.contracts[name])

    def resolve_stage_contract(self, stage: Stage | str) -> dict[str, Any] | None:
        """Return a fully frozen resolved contract dict for a stage, or None.

        A stage resolves a contract when it references a top-level ``contract_ref``
        or declares an inline ``output_schema`` for a one-off shape.
        """
        if isinstance(stage, str):
            stage = self.stage(stage)
        if stage.contract_ref:
            return self.contract(stage.contract_ref)
        if stage.output_schema:
            return freeze_contract({"output_schema": stage.output_schema})
        return None

    def resolve_task_contract(
        self,
        stage: Stage | str,
        contract_ref: str,
    ) -> dict[str, Any]:
        """Resolve a dynamic (expand) task contract by ref, enforcing allow-list."""
        if isinstance(stage, str):
            stage = self.stage(stage)
        ref = str(contract_ref or "").strip()
        if not ref:
            raise ValueError("contract_ref is required for this stage")
        if stage.allowed_contracts and ref not in stage.allowed_contracts:
            raise ValueError(
                f"contract_ref is not allowed for stage {stage.name}: {ref}"
            )
        return self.contract(ref)


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
    return value


def _optional_tool_allow(raw: dict[str, Any], stage_name: str) -> list[str] | None:
    if "tool_allow" not in raw:
        return None
    value = raw.get("tool_allow")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"stage '{stage_name}' tool_allow must be an array of strings")
    names = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    if not names:
        raise ValueError(f"stage '{stage_name}' tool_allow must name at least one tool")
    return names


def _optional_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return dict(value)


def _optional_string_map(value: Any) -> dict[str, str]:
    data = _optional_dict(value)
    return {str(k): str(v) for k, v in data.items()}


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError("expected boolean")


def _check_json_schema(schema: Any, context: str) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise ValueError(f"{context} output_schema must be an object")
    if Draft7Validator is not None:
        try:
            Draft7Validator.check_schema(schema)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{context} output_schema is not a valid JSON schema: {exc}")
    return dict(schema)


def _reject_unknown_keys(
    payload: dict[str, Any],
    known_keys: frozenset[str],
    context: str,
) -> None:
    unknown = sorted(str(key) for key in payload if key not in known_keys)
    if unknown:
        raise ValueError(f"{context} contains unknown field(s): {', '.join(unknown)}")


def _validate_pointer(pointer: Any, context: str) -> str:
    if not isinstance(pointer, str):
        raise ValueError(f"{context} pointer must be a string")
    if pointer and not pointer.startswith("/"):
        raise ValueError(f"{context} pointer must be an RFC6901 pointer (start with '/'): {pointer}")
    return pointer


def _validate_outcome(outcome: Any, context: str) -> dict[str, Any]:
    if not isinstance(outcome, dict):
        raise ValueError(f"{context} outcome must be an object")
    _validate_pointer(outcome.get("pointer"), f"{context} outcome")
    succeeded = outcome.get("succeeded_values")
    blocked = outcome.get("blocked_values")
    if not isinstance(succeeded, list) or not succeeded:
        raise ValueError(f"{context} outcome.succeeded_values must be a non-empty array")
    if blocked is not None and not isinstance(blocked, list):
        raise ValueError(f"{context} outcome.blocked_values must be an array")
    return {
        "pointer": outcome["pointer"],
        "succeeded_values": list(succeeded),
        "blocked_values": list(blocked or []),
    }


def freeze_contract(raw: dict[str, Any], *, name: str = "", context: str = "contract") -> dict[str, Any]:
    """Validate + normalize a contract fragment into a frozen (deep-copied) dict."""
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be an object")
    resolved: dict[str, Any] = {"name": name}

    output_schema = raw.get("output_schema")
    if output_schema is not None:
        resolved["output_schema"] = _check_json_schema(output_schema, context)
    else:
        resolved["output_schema"] = None

    required_artifacts = raw.get("required_artifacts")
    artifacts_map: dict[str, str] = {}
    if required_artifacts is not None:
        if not isinstance(required_artifacts, dict):
            raise ValueError(
                f"{context} required_artifacts must be a label->pointer object"
            )
        for label, pointer in required_artifacts.items():
            artifacts_map[str(label)] = _validate_pointer(
                pointer, f"{context} required_artifacts[{label}]"
            )
    resolved["required_artifacts"] = artifacts_map

    validators = raw.get("semantic_validators")
    validator_list: list[str] = []
    if validators is not None:
        if not isinstance(validators, list) or not all(
            isinstance(item, str) for item in validators
        ):
            raise ValueError(f"{context} semantic_validators must be an array of strings")
        for item in validators:
            if item not in KNOWN_SEMANTIC_VALIDATORS:
                raise ValueError(f"{context} references unknown semantic validator: {item}")
            validator_list.append(item)
    resolved["semantic_validators"] = validator_list

    outcome = raw.get("outcome")
    resolved["outcome"] = _validate_outcome(outcome, context) if outcome is not None else None

    return copy.deepcopy(resolved)


def _load_contracts(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_contracts = data.get("contracts")
    if raw_contracts is None:
        return {}
    if not isinstance(raw_contracts, dict):
        raise ValueError("contracts must be an object mapping name -> contract")
    contracts: dict[str, dict[str, Any]] = {}
    for name, raw in raw_contracts.items():
        contracts[str(name)] = freeze_contract(
            raw, name=str(name), context=f"contract '{name}'"
        )
    return contracts


def _load_stage(raw: dict[str, Any]) -> Stage:
    _reject_unknown_keys(raw, KNOWN_STAGE_KEYS, "stage")
    name = _require_string(raw, "name")
    kind = _require_string(raw, "kind")
    agent_id = _require_string(raw, "agent_id")
    if kind not in VALID_STAGE_KINDS:
        raise ValueError(f"stage kind must be one of: {', '.join(sorted(VALID_STAGE_KINDS))}")
    approval_required = bool(raw.get("approval_required", False))
    plan_approval_required = bool(raw.get("plan_approval_required", False))
    if kind == "expand" and approval_required:
        raise ValueError("expand stages cannot set approval_required")
    if kind in {"task", "compensate"} and plan_approval_required:
        raise ValueError(f"{kind} stages cannot set plan_approval_required")

    retry = dict(raw.get("retry") or {})
    retry.setdefault("max_retries", DEFAULT_MAX_RETRIES)
    retry.setdefault("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    allowed_agents = _string_list(raw.get("allowed_agents"), "allowed_agents")
    if not allowed_agents:
        allowed_agents = [agent_id]
    if kind == "expand" and not allowed_agents:
        raise ValueError("expand stages require allowed_agents")

    required_artifacts = _string_list(raw.get("required_artifacts"), "required_artifacts")
    if required_artifacts:
        warnings.warn(
            (
                f"stage '{name}' uses deprecated stage-level string required_artifacts; "
                "migrate to a declarative contract (contract_ref) with a "
                "required_artifacts label->pointer map"
            ),
            DeprecationWarning,
            stacklevel=2,
        )

    contract_ref = str(raw.get("contract_ref") or "").strip()
    allowed_contracts = _string_list(raw.get("allowed_contracts"), "allowed_contracts")
    output_schema = raw.get("output_schema")
    if output_schema is not None:
        output_schema = _check_json_schema(output_schema, f"stage '{name}'")
    else:
        output_schema = {}

    return Stage(
        name=name,
        kind=kind,
        agent_id=agent_id,
        allowed_agents=allowed_agents,
        allowed_external_effects=_string_list(
            raw.get("allowed_external_effects"), "allowed_external_effects"
        ),
        depends_on=_string_list(raw.get("depends_on"), "depends_on"),
        required_artifacts=required_artifacts,
        skill_hints=_string_list(raw.get("skill_hints"), "skill_hints"),
        retry={
            "max_retries": int(retry["max_retries"]),
            "timeout_seconds": int(retry["timeout_seconds"]),
        },
        approval_required=approval_required,
        plan_approval_required=plan_approval_required,
        input_map=_optional_string_map(raw.get("input_map")),
        on_reject=_optional_dict(raw.get("on_reject")),
        on_block=_optional_dict(raw.get("on_block")),
        on_failure=_optional_dict(raw.get("on_failure")),
        on_complete=_optional_dict(raw.get("on_complete")),
        wait_external=_optional_dict(raw.get("wait_external")),
        join=_optional_dict(raw.get("join")),
        barrier=_optional_dict(raw.get("barrier")),
        present_output=_optional_bool(raw.get("present_output")),
        present_prompt=str(raw.get("present_prompt") or "").strip().lower(),
        contract_ref=contract_ref,
        allowed_contracts=allowed_contracts,
        output_schema=output_schema,
        approval=_load_approval_policy(
            raw.get("approval"), stage_name=name, approval_required=approval_required
        ),
        tool_allow=_optional_tool_allow(raw, name),
    )


def load_skeleton_payload(payload: dict[str, Any]) -> WorkflowSkeleton:
    data = dict(payload)
    _reject_unknown_keys(data, KNOWN_TOP_LEVEL_KEYS, "workflow skeleton")
    workflow_type = _require_string(data, "workflow_type")
    definition_version = _require_string(data, "definition_version")
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ValueError("stages must be a non-empty array")
    expansion = workflow_fragments.expand_fragments(data)
    data = expansion.payload
    stages = [
        replace(_load_stage(stage), origin=origin) if origin else _load_stage(stage)
        for stage, (_, origin) in zip(data["stages"], expansion.index_map, strict=True)
    ]
    if len({stage.name for stage in stages}) != len(stages):
        raise ValueError("stage names must be unique")
    contracts = _load_contracts(data)
    for stage in stages:
        if stage.contract_ref and stage.contract_ref not in contracts:
            raise ValueError(
                f"stage '{stage.name}' references unknown contract_ref: {stage.contract_ref}"
            )
        for ref in stage.allowed_contracts:
            if ref not in contracts:
                raise ValueError(
                    f"stage '{stage.name}' references unknown allowed contract: {ref}"
                )
    return WorkflowSkeleton(
        workflow_type=workflow_type,
        definition_version=definition_version,
        stages=stages,
        contracts=contracts,
        display_name=str(data.get("display_name") or ""),
        promotion=_optional_dict(data.get("promotion")),
        notifications=_optional_dict(data.get("notifications")),
        on_workflow_cancel=_optional_dict(data.get("on_workflow_cancel")),
        session=_optional_dict(data.get("session")),
        bridge=_optional_dict(data.get("bridge")),
        program=_optional_dict(data.get("program")),
        deprecated=bool(data.get("deprecated", False)),
        superseded_by=str(data.get("superseded_by") or ""),
        experimental=bool(data.get("experimental", False)),
        on_workflow_complete=_optional_dict(data.get("on_workflow_complete")),
        includes=expansion.includes,
    )


def load_skeleton(path: str | Path) -> WorkflowSkeleton:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("workflow skeleton must be an object")
    return load_skeleton_payload(data)


def _contract_to_payload(contract: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    output_schema = contract.get("output_schema")
    if output_schema is not None:
        payload["output_schema"] = copy.deepcopy(output_schema)
    required_artifacts = contract.get("required_artifacts") or {}
    if required_artifacts:
        payload["required_artifacts"] = dict(required_artifacts)
    semantic_validators = contract.get("semantic_validators") or []
    if semantic_validators:
        payload["semantic_validators"] = list(semantic_validators)
    outcome = contract.get("outcome")
    if outcome is not None:
        payload["outcome"] = copy.deepcopy(outcome)
    return payload


def _stage_to_payload(stage: Stage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": stage.name,
        "kind": stage.kind,
        "agent_id": stage.agent_id,
        "allowed_agents": list(stage.allowed_agents),
        "allowed_external_effects": list(stage.allowed_external_effects),
        "depends_on": list(stage.depends_on),
        "required_artifacts": list(stage.required_artifacts),
        "skill_hints": list(stage.skill_hints),
        "retry": dict(stage.retry),
        "approval_required": stage.approval_required,
        "plan_approval_required": stage.plan_approval_required,
        "input_map": dict(stage.input_map),
        "on_reject": copy.deepcopy(stage.on_reject),
        "on_failure": copy.deepcopy(stage.on_failure),
        "on_complete": copy.deepcopy(stage.on_complete),
        "wait_external": copy.deepcopy(stage.wait_external),
        "join": copy.deepcopy(stage.join),
        "barrier": copy.deepcopy(stage.barrier),
        "present_prompt": stage.present_prompt,
        "contract_ref": stage.contract_ref,
        "allowed_contracts": list(stage.allowed_contracts),
        "output_schema": copy.deepcopy(stage.output_schema),
    }
    if stage.on_block:
        payload["on_block"] = copy.deepcopy(stage.on_block)
    if stage.approval:
        payload["approval"] = copy.deepcopy(stage.approval)
    if stage.present_output is not None:
        payload["present_output"] = stage.present_output
    if stage.tool_allow is not None:
        payload["tool_allow"] = list(stage.tool_allow)
    return payload


def _collapsed_stages(skeleton: WorkflowSkeleton) -> list[dict[str, Any]]:
    """Stage payloads with each fragment's expanded stages folded back into its include."""
    includes = {include["name"]: include for include in skeleton.includes}
    rewired: dict[str, list[str]] = {}
    for include in skeleton.includes:
        rewired.update(include.get("rewired") or {})
    emitted: set[str] = set()
    payloads: list[dict[str, Any]] = []
    for stage in skeleton.stages:
        if stage.origin and stage.origin in includes:
            if stage.origin not in emitted:
                emitted.add(stage.origin)
                payloads.append(copy.deepcopy(includes[stage.origin]["raw"]))
            continue
        payload = _stage_to_payload(stage)
        if stage.name in rewired:
            payload["depends_on"] = list(rewired[stage.name])
        payloads.append(payload)
    return payloads


def skeleton_to_payload(skeleton: WorkflowSkeleton) -> dict[str, Any]:
    fragment_contracts = {
        key for include in skeleton.includes for key in include.get("contract_keys") or []
    }
    return {
        "workflow_type": skeleton.workflow_type,
        "definition_version": skeleton.definition_version,
        "display_name": skeleton.display_name,
        "promotion": copy.deepcopy(skeleton.promotion),
        "notifications": copy.deepcopy(skeleton.notifications),
        "on_workflow_cancel": copy.deepcopy(skeleton.on_workflow_cancel),
        "session": copy.deepcopy(skeleton.session),
        "bridge": copy.deepcopy(skeleton.bridge),
        "program": copy.deepcopy(skeleton.program),
        "deprecated": skeleton.deprecated,
        "superseded_by": skeleton.superseded_by,
        "contracts": {
            name: _contract_to_payload(contract)
            for name, contract in sorted(skeleton.contracts.items())
            if name not in fragment_contracts
        },
        "stages": _collapsed_stages(skeleton),
        "experimental": skeleton.experimental,
        "on_workflow_complete": copy.deepcopy(skeleton.on_workflow_complete),
    }


def canonical_definition_json(payload: dict[str, Any]) -> str:
    skeleton = load_skeleton_payload(payload)
    canonical_payload = skeleton_to_payload(skeleton)
    return json.dumps(canonical_payload, indent=2, sort_keys=True) + "\n"


def validate_proposed_tasks(
    skeleton: WorkflowSkeleton,
    stage: Stage,
    proposed_tasks: list[dict[str, Any]],
    existing_step_names: set[str],
) -> list[dict[str, Any]]:
    if stage.kind != "expand":
        raise ValueError("only expand stages can validate proposed tasks")
    if not isinstance(proposed_tasks, list) or not proposed_tasks:
        raise ValueError("proposed_tasks must be a non-empty array")

    validated: list[dict[str, Any]] = []
    seen = set(existing_step_names)
    for task in proposed_tasks:
        if not isinstance(task, dict):
            raise ValueError("each proposed task must be an object")
        step_name = _require_string(task, "step_name")
        agent_id = _require_string(task, "agent_id")
        if step_name in seen:
            raise ValueError(f"duplicate proposed step_name: {step_name}")
        if agent_id not in stage.allowed_agents:
            raise ValueError(f"agent_id is not allowed for stage {stage.name}: {agent_id}")
        if "input" not in task or not isinstance(task.get("input"), dict):
            raise ValueError("proposed task input is required")
        external_effects = _string_list(task.get("external_effects"), "external_effects")
        for effect in external_effects:
            if effect not in stage.allowed_external_effects:
                raise ValueError(f"external effect is not allowed: {effect}")

        normalized = dict(task)
        normalized["workflow_type"] = skeleton.workflow_type
        normalized["definition_version"] = skeleton.definition_version
        normalized["max_retries"] = int(task.get("max_retries", stage.retry["max_retries"]))
        normalized.setdefault("external_effects", external_effects)

        contract_ref = str(task.get("contract_ref") or "").strip()
        if stage.allowed_contracts:
            if not contract_ref:
                raise ValueError(
                    f"proposed task must include a contract_ref for stage {stage.name}"
                )
            contract = skeleton.resolve_task_contract(stage, contract_ref)
            normalized["contract_ref"] = contract_ref
            normalized["contract"] = contract
        elif contract_ref:
            # No allow-list configured: accept the ref if it resolves globally.
            normalized["contract_ref"] = contract_ref
            if contract_ref in skeleton.contracts:
                normalized["contract"] = skeleton.contract(contract_ref)

        seen.add(step_name)
        validated.append(normalized)
    return validated
