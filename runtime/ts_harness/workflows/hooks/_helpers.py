"""Shared helpers for workflow hooks."""

from __future__ import annotations

from typing import Any

from ts_harness.memory.client import MemoryClient


def load_workflow_payload(client: MemoryClient, workflow_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = client.get_workflow(workflow_id)
    workflow = payload.get("workflow") or {}
    tasks = list(payload.get("tasks") or [])
    return workflow, tasks


def create_tasks_bulk(
    client: MemoryClient,
    *,
    workflow_id: str,
    workflow_type: str,
    tenant_id: str,
    tasks: list[dict[str, Any]],
    actor: str,
) -> list[str]:
    result = client.create_tasks_bulk(
        {
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "tenant_id": tenant_id,
            "tasks": tasks,
            "actor": actor,
        },
    )
    return [str(task_id) for task_id in (result.get("task_ids") or [])]
