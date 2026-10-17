"""Backward-compatible PRD session helpers."""

from __future__ import annotations

from ts_harness.agent.sessions import SessionRecord
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.workflow_session import (
    set_workflow_phase_for_workflow,
    sync_workflow_session_for_workflow,
    sync_workflow_session_phase,
)


def sync_prd_session_phase(session: SessionRecord, *, tenant_id: str | None = None) -> str | None:
    workflow_type = str((session.metadata or {}).get("workflow_type") or "prd_request")
    skeleton = load_skeleton(workflow_type)
    return sync_workflow_session_phase(session, skeleton, tenant_id=tenant_id)


def set_prd_phase_for_workflow(workflow_id: str, phase: str, *, tenant_id: str = "default") -> None:
    skeleton = load_skeleton("prd_request")
    set_workflow_phase_for_workflow(workflow_id, phase, skeleton, tenant_id=tenant_id)


def sync_prd_session_for_workflow(workflow_id: str, *, tenant_id: str = "default") -> None:
    skeleton = load_skeleton("prd_request")
    sync_workflow_session_for_workflow(workflow_id, skeleton, tenant_id=tenant_id)
