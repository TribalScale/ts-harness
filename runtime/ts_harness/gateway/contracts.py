"""Runtime HTTP contract types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid


@dataclass
class RuntimeRequest:
    tenant_id: str
    actor: dict[str, Any] | str
    source: str
    correlation_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DispatchResult:
    status: str  # succeeded | blocked | failed
    output: dict[str, Any] = field(default_factory=dict)
    runtime_task_id: str | None = None
    proposed_tasks: list[dict[str, Any]] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.output:
            result["output"] = self.output
        if self.runtime_task_id:
            result["runtime_task_id"] = self.runtime_task_id
        if self.proposed_tasks is not None:
            result["proposed_tasks"] = self.proposed_tasks
        if self.error:
            result["error"] = self.error
        return result


def new_runtime_task_id() -> str:
    return str(uuid.uuid4())


def normalize_runtime_body(body: dict[str, Any], *, default_tenant: str) -> RuntimeRequest:
    tenant_id = body.get("tenant_id") or default_tenant
    return RuntimeRequest(
        tenant_id=str(tenant_id),
        actor=body.get("actor") or "system",
        source=str(body.get("source") or "workflow"),
        correlation_id=str(body.get("correlation_id") or uuid.uuid4()),
        payload=body,
    )
