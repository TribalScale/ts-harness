"""Batch claims of ready workflow tasks per lane, and lease renewal for running tasks."""

from __future__ import annotations

import json
import uuid
from typing import Any

LANES = frozenset({"fast", "agent", "coding"})
MAX_BATCH = 100
DEFAULT_LEASE_SECONDS = 300


def _int(value: Any, name: str, *, default: int, low: int, high: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return max(low, min(high, number))


def _uuid(value: Any, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def claim_batch(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Claim up to ``limit`` ready tasks of ``lane`` for ``runner``; at most one per workflow."""
    lane = str(body.get("lane") or "agent").strip()
    if lane not in LANES:
        raise ValueError(f"lane must be one of {sorted(LANES)}")
    runner = str(body.get("runner") or "").strip()
    if not runner:
        raise ValueError("runner is required")
    limit = _int(body.get("limit"), "limit", default=10, low=0, high=MAX_BATCH)
    lease = _int(body.get("lease_seconds"), "lease_seconds", default=DEFAULT_LEASE_SECONDS, low=10, high=86400)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM claim_ready_workflow_tasks(%s, %s, %s, %s)",
            (runner, lane, limit, lease),
        )
        tasks = list(cur.fetchall())
        for task in tasks:
            cur.execute(
                """
                INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, occurred_at)
                VALUES (uuid_generate_v4(), %s, %s, 'workflow_task_claimed', %s, %s::jsonb, now())
                """,
                (
                    str(task.get("workflow_id") or task["id"]),
                    runner,
                    f"Workflow task claimed by {task.get('agent_id')}",
                    json.dumps({"task_id": str(task["id"]), "agent_id": task.get("agent_id"), "lane": lane}),
                ),
            )
    conn.commit()
    return {"lane": lane, "tasks": tasks}


def renew_lease(conn, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Extend a running task's lease; ``renewed`` is false when another runner holds it or it finished."""
    task_id = _uuid(task_id, "task id")
    runner = str(body.get("runner") or "").strip()
    if not runner:
        raise ValueError("runner is required")
    lease = _int(body.get("lease_seconds"), "lease_seconds", default=DEFAULT_LEASE_SECONDS, low=10, high=86400)
    with conn.cursor() as cur:
        cur.execute("SELECT renew_workflow_task_lease(%s, %s, %s) AS renewed", (task_id, runner, lease))
        renewed = bool((cur.fetchone() or {}).get("renewed"))
    conn.commit()
    return {"id": task_id, "renewed": renewed}
