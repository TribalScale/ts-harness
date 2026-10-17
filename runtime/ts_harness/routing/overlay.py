"""Managed routing overlay merge and validation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ts_harness.config.schema import (
    RoutingConfig,
    RoutingIntentPolicy,
    RoutingIntentRule,
)


def intent_to_dict(rule: RoutingIntentRule) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": rule.id,
        "signals": list(rule.signals or []),
        "exclude": list(rule.exclude or []),
    }
    if rule.workflow_type:
        payload["workflow_type"] = rule.workflow_type
    if rule.program_type:
        payload["program_type"] = rule.program_type
    if rule.entry_phase:
        payload["entry_phase"] = rule.entry_phase
    if rule.delegate_agent:
        payload["delegate_agent"] = rule.delegate_agent
    if rule.orchestrator_hint:
        payload["orchestrator_hint"] = rule.orchestrator_hint
    if rule.policy is not None:
        payload["policy"] = {
            "mode": rule.policy.mode,
            "deny_tools": list(rule.policy.deny_tools or []),
            "deny_start_workflow_types": list(rule.policy.deny_start_workflow_types or []),
            "allow_program_types": list(rule.policy.allow_program_types or []),
        }
    return payload


def intent_from_dict(raw: dict[str, Any]) -> RoutingIntentRule:
    policy_raw = raw.get("policy")
    policy = None
    if isinstance(policy_raw, dict):
        policy = RoutingIntentPolicy(
            mode=str(policy_raw.get("mode") or "require_workflow"),
            deny_tools=[str(item) for item in policy_raw.get("deny_tools") or []],
            deny_start_workflow_types=[
                str(item) for item in policy_raw.get("deny_start_workflow_types") or []
            ],
            allow_program_types=[
                str(item) for item in policy_raw.get("allow_program_types") or []
            ],
        )
    return RoutingIntentRule(
        id=str(raw.get("id") or "").strip(),
        signals=[str(item) for item in raw.get("signals") or []],
        exclude=[str(item) for item in raw.get("exclude") or []],
        workflow_type=(
            str(raw["workflow_type"]).strip() if raw.get("workflow_type") else None
        ),
        program_type=(
            str(raw["program_type"]).strip() if raw.get("program_type") else None
        ),
        entry_phase=(
            str(raw["entry_phase"]).strip() if raw.get("entry_phase") else None
        ),
        delegate_agent=(
            str(raw["delegate_agent"]).strip() if raw.get("delegate_agent") else None
        ),
        orchestrator_hint=(
            str(raw["orchestrator_hint"]) if raw.get("orchestrator_hint") else None
        ),
        policy=policy,
    )


def merge_routing_intents(
    yaml_intents: list[RoutingIntentRule],
    overlay_intents: list[RoutingIntentRule],
) -> list[RoutingIntentRule]:
    by_id: dict[str, RoutingIntentRule] = {}
    order: list[str] = []
    for rule in yaml_intents:
        by_id[rule.id] = rule
        order.append(rule.id)
    for rule in overlay_intents:
        if rule.id not in by_id:
            order.append(rule.id)
        by_id[rule.id] = rule
    return [by_id[intent_id] for intent_id in order if intent_id in by_id]


def signal_collision_warnings(
    intents: list[RoutingIntentRule],
) -> list[dict[str, str]]:
    """Report shared signals; collisions involving a program or workflow intent are errors."""
    signal_owners: dict[str, list[str]] = {}
    starts_work: dict[str, bool] = {}
    for rule in intents:
        starts_work[rule.id] = bool(rule.program_type or rule.workflow_type)
        for signal in rule.signals:
            key = str(signal).strip().lower()
            if not key:
                continue
            signal_owners.setdefault(key, []).append(rule.id)
    warnings: list[dict[str, str]] = []
    for signal, owners in sorted(signal_owners.items()):
        unique_owners = sorted(set(owners))
        if len(unique_owners) > 1:
            blocking = any(starts_work.get(owner) for owner in unique_owners)
            warnings.append(
                {
                    "severity": "error" if blocking else "warning",
                    "code": "signal_collision",
                    "path": "$.intents",
                    "message": (
                        f"signal {signal!r} is shared by intents: "
                        + ", ".join(unique_owners)
                        + (
                            "; intents that start a program or workflow need unique signals"
                            if blocking
                            else ""
                        )
                    ),
                }
            )
    return warnings


def validate_overlay_payload(
    payload: dict[str, Any],
    *,
    known_program_types: set[str],
    known_workflow_types: set[str],
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return None, [
            {
                "severity": "error",
                "code": "invalid_payload",
                "path": "$",
                "message": "routing definition must be an object",
            }
        ]
    if str(payload.get("definition_type") or "") != "channel_routing":
        issues.append(
            {
                "severity": "error",
                "code": "invalid_definition_type",
                "path": "$.definition_type",
                "message": "definition_type must be channel_routing",
            }
        )
    if not str(payload.get("definition_version") or "").strip():
        issues.append(
            {
                "severity": "error",
                "code": "missing_version",
                "path": "$.definition_version",
                "message": "definition_version is required",
            }
        )
    raw_intents = payload.get("intents")
    if not isinstance(raw_intents, list):
        issues.append(
            {
                "severity": "error",
                "code": "invalid_intents",
                "path": "$.intents",
                "message": "intents must be an array",
            }
        )
        return None, issues

    parsed: list[RoutingIntentRule] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_intents):
        if not isinstance(item, dict):
            issues.append(
                {
                    "severity": "error",
                    "code": "invalid_intent",
                    "path": f"$.intents[{index}]",
                    "message": "intent must be an object",
                }
            )
            continue
        rule = intent_from_dict(item)
        if not rule.id:
            issues.append(
                {
                    "severity": "error",
                    "code": "missing_intent_id",
                    "path": f"$.intents[{index}].id",
                    "message": "intent id is required",
                }
            )
            continue
        if rule.id in seen_ids:
            issues.append(
                {
                    "severity": "error",
                    "code": "duplicate_intent_id",
                    "path": f"$.intents[{index}].id",
                    "message": f"duplicate intent id: {rule.id}",
                }
            )
        seen_ids.add(rule.id)
        if not rule.signals:
            issues.append(
                {
                    "severity": "error",
                    "code": "missing_signals",
                    "path": f"$.intents[{index}].signals",
                    "message": "intent signals are required",
                }
            )
        if rule.program_type and known_program_types and rule.program_type not in known_program_types:
            issues.append(
                {
                    "severity": "error",
                    "code": "unknown_program_type",
                    "path": f"$.intents[{index}].program_type",
                    "message": f"unknown program_type: {rule.program_type}",
                }
            )
        if rule.workflow_type and known_workflow_types and rule.workflow_type not in known_workflow_types:
            issues.append(
                {
                    "severity": "error",
                    "code": "unknown_workflow_type",
                    "path": f"$.intents[{index}].workflow_type",
                    "message": f"unknown workflow_type: {rule.workflow_type}",
                }
            )
        if not rule.program_type and not rule.workflow_type and not rule.delegate_agent:
            issues.append(
                {
                    "severity": "error",
                    "code": "missing_target",
                    "path": f"$.intents[{index}]",
                    "message": "intent requires program_type, workflow_type, or delegate_agent",
                }
            )
        parsed.append(rule)

    issues.extend(signal_collision_warnings(parsed))
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors:
        return None, issues

    canonical = {
        "definition_type": "channel_routing",
        "definition_version": str(payload.get("definition_version") or "").strip(),
        "intents": [intent_to_dict(rule) for rule in parsed],
    }
    return canonical, issues


def apply_overlay_to_routing(
    routing: RoutingConfig | None,
    overlay_intents: list[RoutingIntentRule],
) -> RoutingConfig:
    base = routing or RoutingConfig()
    merged = merge_routing_intents(list(base.intents or []), overlay_intents)
    return replace(base, intents=merged)


def overlay_intents_from_payload(payload: dict[str, Any] | None) -> list[RoutingIntentRule]:
    if not isinstance(payload, dict):
        return []
    intents = payload.get("intents")
    if not isinstance(intents, list):
        return []
    return [
        intent_from_dict(item)
        for item in intents
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]


def routing_config_as_dicts(routing: RoutingConfig | None) -> list[dict[str, Any]]:
    if routing is None:
        return []
    return [intent_to_dict(rule) for rule in routing.intents]
