"""Session metadata key helpers (generic keys with legacy read compat)."""

from __future__ import annotations

from typing import Any


_LEGACY_PHASE = "prd_phase"
_GENERIC_PHASE = "workflow_phase"
_LEGACY_MODE_PRD = "prd_linked"
_LEGACY_MODE_DELIVERY = "delivery_linked"
_GENERIC_MODE = "workflow_linked"
_GENERIC_BRIDGE_MODE = "bridge_linked"
_LEGACY_ARTIFACT = "approved_prd_artifact"
_GENERIC_ARTIFACT = "approved_artifact"
_REVIEW_ARTIFACT = "review_artifact"
_SPECIALIST_HANDOFF = "specialist_handoff"
_LEGACY_SOURCE = "source_prd_workflow_id"
_GENERIC_SOURCE = "source_workflow_id"
_LEGACY_PENDING = "pending_continue_delivery"
_GENERIC_PENDING = "pending_bridge"


def read_workflow_phase(metadata: dict[str, Any] | None) -> str | None:
    data = metadata or {}
    phase = data.get(_GENERIC_PHASE) or data.get(_LEGACY_PHASE)
    return str(phase) if phase else None


def write_workflow_phase(metadata: dict[str, Any], phase: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated[_GENERIC_PHASE] = phase
    updated.pop(_LEGACY_PHASE, None)
    return updated


def read_session_mode(metadata: dict[str, Any] | None) -> str | None:
    data = metadata or {}
    mode = str(data.get("mode") or "")
    if mode == _LEGACY_MODE_PRD:
        return _GENERIC_MODE
    if mode == _LEGACY_MODE_DELIVERY:
        return _GENERIC_BRIDGE_MODE
    return mode or None


def write_session_mode(metadata: dict[str, Any], mode: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated["mode"] = mode
    return updated


def read_approved_artifact(metadata: dict[str, Any] | None) -> Any:
    data = metadata or {}
    return data.get(_GENERIC_ARTIFACT) or data.get(_LEGACY_ARTIFACT)


def write_approved_artifact(metadata: dict[str, Any], artifact: Any) -> dict[str, Any]:
    updated = dict(metadata)
    updated[_GENERIC_ARTIFACT] = artifact
    updated.pop(_LEGACY_ARTIFACT, None)
    return updated


def read_review_artifact(metadata: dict[str, Any] | None) -> Any:
    data = metadata or {}
    return data.get(_REVIEW_ARTIFACT)


def write_review_artifact(metadata: dict[str, Any], artifact: Any) -> dict[str, Any]:
    updated = dict(metadata)
    updated[_REVIEW_ARTIFACT] = artifact
    return updated


def read_specialist_handoff(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    data = metadata or {}
    handoff = data.get(_SPECIALIST_HANDOFF)
    return handoff if isinstance(handoff, dict) else None


def write_specialist_handoff(
    metadata: dict[str, Any],
    *,
    step_name: str,
    agent_id: str,
    output: dict[str, Any],
) -> dict[str, Any]:
    updated = write_review_artifact(metadata, output)
    updated[_SPECIALIST_HANDOFF] = {
        "step_name": step_name,
        "agent_id": agent_id,
        "output": output,
    }
    return updated


def read_source_workflow_id(metadata: dict[str, Any] | None) -> str:
    data = metadata or {}
    return str(data.get(_GENERIC_SOURCE) or data.get(_LEGACY_SOURCE) or data.get("workflow_id") or "")


def write_source_workflow_id(metadata: dict[str, Any], workflow_id: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated[_GENERIC_SOURCE] = workflow_id
    updated.pop(_LEGACY_SOURCE, None)
    return updated


def read_pending_bridge(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    data = metadata or {}
    pending = data.get(_GENERIC_PENDING) or data.get(_LEGACY_PENDING)
    return pending if isinstance(pending, dict) else None


def write_pending_bridge(metadata: dict[str, Any], pending: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(metadata)
    if pending is None:
        updated.pop(_GENERIC_PENDING, None)
        updated.pop(_LEGACY_PENDING, None)
    else:
        updated[_GENERIC_PENDING] = pending
        updated.pop(_LEGACY_PENDING, None)
    return updated


def is_workflow_linked(metadata: dict[str, Any] | None) -> bool:
    mode = read_session_mode(metadata) or str((metadata or {}).get("mode") or "")
    return mode in {_GENERIC_MODE, _GENERIC_BRIDGE_MODE, _LEGACY_MODE_PRD, _LEGACY_MODE_DELIVERY, "workflow_linked"}


def is_bridge_linked(metadata: dict[str, Any] | None) -> bool:
    mode = read_session_mode(metadata) or str((metadata or {}).get("mode") or "")
    return mode in {_GENERIC_BRIDGE_MODE, _LEGACY_MODE_DELIVERY}


def read_program_id(metadata: dict[str, Any] | None) -> str:
    data = metadata or {}
    return str(data.get("program_id") or "")


def write_program_id(metadata: dict[str, Any], program_id: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated["program_id"] = program_id
    return updated


def read_program_phase(metadata: dict[str, Any] | None) -> str | None:
    data = metadata or {}
    phase = data.get("program_phase")
    return str(phase) if phase else None


def write_program_phase(metadata: dict[str, Any], phase: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated["program_phase"] = phase
    return updated


def read_active_workflow_id(metadata: dict[str, Any] | None) -> str:
    data = metadata or {}
    return str(data.get("active_workflow_id") or data.get("workflow_id") or "")


def write_active_workflow_id(metadata: dict[str, Any], workflow_id: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated["active_workflow_id"] = workflow_id
    updated["workflow_id"] = workflow_id
    return updated


def read_program_artifacts(metadata: dict[str, Any] | None) -> dict[str, Any]:
    data = metadata or {}
    artifacts = data.get("program_artifacts")
    return dict(artifacts) if isinstance(artifacts, dict) else {}


def write_program_artifacts(metadata: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    updated = dict(metadata)
    updated["program_artifacts"] = dict(artifacts)
    return updated


def read_pending_program(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    data = metadata or {}
    pending = data.get("pending_program")
    return pending if isinstance(pending, dict) else None


def write_pending_program(metadata: dict[str, Any], pending: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(metadata)
    if pending is None:
        updated.pop("pending_program", None)
    else:
        updated["pending_program"] = pending
    return updated


def read_pending_program_advance(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    data = metadata or {}
    pending = data.get("pending_program_advance")
    return pending if isinstance(pending, dict) else None


def write_pending_program_advance(
    metadata: dict[str, Any],
    pending: dict[str, Any] | None,
) -> dict[str, Any]:
    updated = dict(metadata)
    if pending is None:
        updated.pop("pending_program_advance", None)
    else:
        updated["pending_program_advance"] = pending
    return updated


_PROGRAM_SESSION_KEYS = (
    "program_id",
    "program_type",
    "program_phase",
    "program_artifacts",
    "workflow_id",
    "active_workflow_id",
    "workflow_type",
    _GENERIC_PHASE,
    _LEGACY_PHASE,
    _GENERIC_SOURCE,
    _LEGACY_SOURCE,
    _GENERIC_ARTIFACT,
    _LEGACY_ARTIFACT,
    _REVIEW_ARTIFACT,
    _SPECIALIST_HANDOFF,
    _GENERIC_PENDING,
    _LEGACY_PENDING,
    "pending_workflow",
    "pending_delegate",
    "pending_program",
    "pending_program_advance",
    "presentation_doc_id",
    "presentation_doc_url",
    _GENERIC_MODE,
    _GENERIC_BRIDGE_MODE,
    _LEGACY_MODE_PRD,
    _LEGACY_MODE_DELIVERY,
)
_LINKED_MODES = frozenset({_GENERIC_MODE, _GENERIC_BRIDGE_MODE, _LEGACY_MODE_PRD, _LEGACY_MODE_DELIVERY})


def clear_program_session(metadata: dict[str, Any]) -> dict[str, Any]:
    """Unlink the session from its program and workflows, keeping the channel binding."""
    updated = {key: value for key, value in metadata.items() if key not in _PROGRAM_SESSION_KEYS}
    if str(updated.get("mode") or "") in _LINKED_MODES:
        updated.pop("mode")
    if metadata.get("program_id"):
        updated["previous_program_id"] = metadata["program_id"]
    return updated


def write_program_session(
    metadata: dict[str, Any],
    *,
    program_id: str,
    program_phase: str,
    workflow_id: str,
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updated = write_program_id(metadata, program_id)
    updated = write_program_phase(updated, program_phase)
    updated = write_active_workflow_id(updated, workflow_id)
    if artifacts is not None:
        updated = write_program_artifacts(updated, artifacts)
    return updated


def refreshed_program_link(metadata: dict[str, Any] | None, memory: Any) -> dict[str, Any] | None:
    """Metadata re-linked to the program's active workflow when the chat still points at a
    finished one, else None.

    A phase reached automatically (work orders -> file in Jira) starts a new workflow in the
    workflow runner, which cannot update the chat session another process holds. Replies would
    then look for a gate on the finished workflow and find none.
    """
    data = dict(metadata or {})
    program_id = read_program_id(data)
    if not program_id or memory is None:
        return None
    status = memory.get_program_status(program_id) or {}
    active = str(status.get("active_workflow_id") or "")
    linked = str(data.get("workflow_id") or "")
    if not active or active == linked:
        return None
    if linked:
        workflow = (memory.get_workflow(linked) or {}).get("workflow") or {}
        if str(workflow.get("status") or "") == "running":
            return None
    phase = str(status.get("current_phase") or read_program_phase(data) or "")
    return write_program_session(data, program_id=program_id, program_phase=phase, workflow_id=active)
