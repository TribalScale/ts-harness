"""Generic specialist output presentation rules for workflow stages."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.artifacts import artifact_steps
from ts_harness.workflows.workflow_session import get_phase_tracking

VALID_PRESENT_PROMPTS = frozenset({"approval", "inform", "intake", "merge", "none"})


def orchestrator_agent_id(skeleton: Any) -> str:
    session_block = dict(getattr(skeleton, "session", None) or {})
    return str(session_block.get("orchestrator_agent_id") or "orchestrator")


def should_present_specialist_output(
    stage: Any | None,
    skeleton: Any,
    *,
    step_name: str = "",
) -> bool:
    """Default: present specialist task output; opt out per stage or orchestrator steps."""
    resolved_step = step_name or str(getattr(stage, "name", "") or "")
    if stage is None:
        if resolved_step in artifact_steps(skeleton):
            return True
        return False

    present_output = getattr(stage, "present_output", None)
    if present_output is False:
        return False
    if present_output is True:
        return True

    if str(getattr(stage, "agent_id", "") or "") == orchestrator_agent_id(skeleton):
        return False
    if str(getattr(stage, "kind", "") or "") == "expand":
        return False
    return True


def _actionable_gate_ids(
    tasks: list[dict[str, Any]],
    skeleton: Any,
    *,
    presenting_step_name: str = "",
) -> set[str]:
    tracking = get_phase_tracking(skeleton)
    gate_steps = set(tracking.gate_steps) if tracking is not None else set()
    by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    by_step_name = {
        str(task.get("step_name") or ""): task
        for task in tasks
        if task.get("step_name")
    }
    presenting = (presenting_step_name or "").strip()
    actionable: set[str] = set()
    for task in tasks:
        step_name = str(task.get("step_name") or "")
        if gate_steps and step_name not in gate_steps:
            continue
        if not task.get("approval_required"):
            continue
        if str(task.get("approval_status") or "").lower() != "pending":
            continue
        if str(task.get("status") or "").lower() != "pending":
            continue
        deps = [str(dep_id) for dep_id in (task.get("depends_on") or [])]
        if all(
            _dependency_satisfied(
                dep_id,
                by_id=by_id,
                by_step_name=by_step_name,
                presenting_step_name=presenting,
            )
            for dep_id in deps
        ):
            actionable.add(str(task["id"]))
    return actionable


def _dependency_satisfied(
    dep_id: str,
    *,
    by_id: dict[str, dict[str, Any]],
    by_step_name: dict[str, dict[str, Any]],
    presenting_step_name: str,
) -> bool:
    dep_task = by_id.get(dep_id) or by_step_name.get(dep_id)
    if dep_task is None:
        return False
    status = str(dep_task.get("status") or "").lower()
    if status == "succeeded":
        return True
    step_name = str(dep_task.get("step_name") or "")
    return bool(presenting_step_name and step_name == presenting_step_name)


def _artifact_step_present_prompt(skeleton: Any, step_name: str) -> str:
    session_block = dict(getattr(skeleton, "session", None) or {})
    prompts = session_block.get("artifact_step_prompts")
    if isinstance(prompts, dict):
        value = str(prompts.get(step_name) or "").strip().lower()
        if value in VALID_PRESENT_PROMPTS:
            return value
    for stage in getattr(skeleton, "stages", []) or []:
        if str(getattr(stage, "name", "") or "") != step_name:
            continue
        explicit = str(getattr(stage, "present_prompt", "") or "").strip().lower()
        if explicit in VALID_PRESENT_PROMPTS:
            return explicit
    for stage in getattr(skeleton, "stages", []) or []:
        revise_cfg = dict((getattr(stage, "on_reject", None) or {}).get("revise") or {})
        if str(revise_cfg.get("step_name") or "") == step_name:
            explicit = str(revise_cfg.get("present_prompt") or "").strip().lower()
            if explicit in VALID_PRESENT_PROMPTS:
                return explicit
    return ""


def resolve_present_prompt(
    stage: Any | None,
    skeleton: Any,
    tasks: list[dict[str, Any]] | None = None,
    *,
    presenting_step_name: str = "",
) -> str:
    step_name = presenting_step_name or str(getattr(stage, "name", "") or "")
    artifact_prompt = _artifact_step_present_prompt(skeleton, step_name)
    if artifact_prompt:
        return artifact_prompt

    explicit = str(getattr(stage, "present_prompt", "") or "").strip().lower()
    if explicit in VALID_PRESENT_PROMPTS:
        return explicit

    if step_name in artifact_steps(skeleton) and _actionable_gate_ids(
        tasks or [],
        skeleton,
        presenting_step_name=step_name,
    ):
        return "approval"
    if getattr(stage, "approval_required", False):
        return "approval"
    return "inform"


def infer_present_body(schema: Any, prefix: str = "") -> str:
    """First string property ending in ``_markdown``, required properties first, searching nested objects."""
    if not isinstance(schema, dict):
        return ""
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [str(name) for name in schema.get("required") or [] if name in properties]
    ordered = required + [str(name) for name in properties if name not in required]
    for name in ordered:
        prop = properties.get(name) or {}
        if name.endswith("_markdown") and isinstance(prop, dict) and prop.get("type") == "string":
            return f"{prefix}{name}"
    for name in ordered:
        prop = properties.get(name) or {}
        if isinstance(prop, dict) and prop.get("type") == "object":
            nested = infer_present_body(prop, f"{prefix}{name}.")
            if nested:
                return nested
    for branch in list(schema.get("anyOf") or []) + list(schema.get("oneOf") or []):
        nested = infer_present_body(branch, prefix)
        if nested:
            return nested
    return ""


def schema_string_path_status(schema: Any, path: str) -> str:
    """``"string"`` when ``path`` names a string field, ``"other"`` for a non-string, ``"missing"`` otherwise.

    Schemas that declare no properties at that level are open, so any path counts as ``"string"``.
    """
    if not isinstance(schema, dict):
        return "string"
    head, _, rest = path.partition(".")
    branches = list(schema.get("anyOf") or []) + list(schema.get("oneOf") or [])
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not properties and not branches:
        return "string"
    statuses: list[str] = []
    if head in properties:
        prop = properties[head]
        if rest:
            statuses.append(schema_string_path_status(prop, rest))
        elif not isinstance(prop, dict) or prop.get("type") in (None, "string"):
            statuses.append("string")
        else:
            statuses.append("other")
    for branch in branches:
        statuses.append(schema_string_path_status(branch, path))
    for status in ("string", "other"):
        if status in statuses:
            return status
    return "missing"


def stage_presentation(skeleton: Any, step_name: str) -> dict[str, str]:
    """Where a stage's channel message body and title come from, per the workflow definition."""
    try:
        stage = skeleton.stage(step_name)
    except (AttributeError, KeyError, ValueError):
        return {}
    contract = {}
    try:
        contract = skeleton.resolve_stage_contract(stage) or {}
    except (AttributeError, KeyError, ValueError):
        contract = {}
    configured = dict(contract.get("presentation") or {})
    body = str(getattr(stage, "present_body", "") or configured.get("body") or "")
    body = body or infer_present_body(contract.get("output_schema"))
    out = {"body": body} if body else {}
    if configured.get("title"):
        out["title"] = str(configured["title"])
    return out


def workflow_step_presentation(workflow_type: str, step_name: str) -> dict[str, str]:
    if not workflow_type or not step_name:
        return {}
    try:
        from ts_harness.workflows.materialize import load_skeleton

        return stage_presentation(load_skeleton(workflow_type), step_name)
    except (FileNotFoundError, LookupError, ValueError):
        return {}
