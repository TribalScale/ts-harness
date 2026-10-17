"""Dynamic PRD revision task materialization on approval reject.

Deprecated: use WorkflowEngine.handle_reject with skeleton on_reject hooks.
"""

from __future__ import annotations

import warnings
from typing import Any

from ts_harness.memory.client import MemoryClient, get_memory_client
from ts_harness.workflows.engine import get_workflow_engine


def materialize_prd_revision(
    *,
    workflow_id: str,
    gate_task_id: str,
    comments: str,
    actor: str,
    tenant_id: str = "default",
    client: MemoryClient | None = None,
) -> dict[str, Any]:
    warnings.warn(
        "materialize_prd_revision is deprecated; use WorkflowEngine.handle_reject",
        DeprecationWarning,
        stacklevel=2,
    )
    memory = client or get_memory_client()
    payload = memory.get_workflow(workflow_id)
    gate = next((task for task in (payload.get("tasks") or []) if str(task.get("id")) == gate_task_id), None)
    if gate is None:
        raise ValueError("gate task not found in workflow")
    engine = get_workflow_engine()
    return engine.handle_reject(
        workflow_id=workflow_id,
        gate_task=gate,
        comments=comments,
        actor=actor,
        tenant_id=tenant_id,
    )
