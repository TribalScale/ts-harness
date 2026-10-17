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
    approve_stage = None
    try:
        approve_stage = skeleton.stage("approve_prd")
    except (AttributeError, KeyError):
        approve_stage = None
    if approve_stage is not None:
        revise_cfg = dict((approve_stage.on_reject or {}).get("revise") or {})
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
