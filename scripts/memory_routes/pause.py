"""Pause and resume on the memory API side (see ``ts_harness.workflows.pause``).

The pause document lives in ``runtime_kv`` (namespace ``coding-pause``, key ``state``); these
routes read it inside their own transaction so the decision and the write agree:

* ``hold_paused_task``: park a task until resume (blocked on ``workflow.resumed`` with the
  ``_pause_hold`` marker), in one statement;
* ``list_paused_tasks``: every held task of a tenant, and whether a pause still covers it;
* ``release_paused_tasks``: put every held task no pause covers any more back to pending, its
  input intact (``_pause_hold`` moves to ``_pause_last`` with who resumed and when);
* ``shift_wait_deadlines``: move the scope's wait deadlines on by the time it was paused;
* ``held_job_ids``: the queued coding jobs a pause covers, which the claim skips (never cancels).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

NAMESPACE = "coding-pause"
KEY = "state"
EVENT_TYPE = "workflow.resumed"
WAIT_ERROR = f"awaiting_external:{EVENT_TYPE}"
HOLD_KEY = "_pause_hold"
LAST_KEY = "_pause_last"
PAUSED_HELD_REASON = "paused"
GLOBAL_HELD_REASON = "coding paused for everything"
_OPEN = ("succeeded", "failed", "cancelled", "superseded")

# Workflows a pause covers: the paused workflows, every workflow of a paused program or of one of
# its sub-programs, and every workflow below any of those (a queue's tickets, a wave's work orders).
_TREE = """
WITH RECURSIVE progs AS (
    SELECT id FROM delivery_programs WHERE id::text = ANY(%(programs)s)
  UNION
    SELECT p.id FROM delivery_programs p JOIN progs ON p.parent_program_id = progs.id
), tree AS (
    SELECT id FROM workflows
     WHERE id::text = ANY(%(workflows)s) OR program_id IN (SELECT id FROM progs)
  UNION
    SELECT w.id FROM workflows w JOIN tree ON w.parent_workflow_id = tree.id
)
"""


def _uuid(value: Any, field: str = "id") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _uuids(values: Any) -> list[str]:
    out = []
    for value in values or []:
        try:
            out.append(str(uuid.UUID(str(value))))
        except (TypeError, ValueError):
            continue
    return out


def _audit(cur, action_type: str, summary: str, details: dict[str, Any], actor: str) -> None:
    cur.execute(
        """
        INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, occurred_at)
        VALUES (uuid_generate_v4(), %s, %s, %s, %s, %s::jsonb, now())
        """,
        (str(details.get("workflow_id") or details.get("task_id") or "pause"), actor, action_type, summary,
         json.dumps(details, default=str)),
    )


def read_state(cur) -> dict[str, Any]:
    cur.execute("SELECT value FROM runtime_kv WHERE namespace = %s AND key = %s", (NAMESPACE, KEY))
    row = cur.fetchone()
    value = (row or {}).get("value") if row else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    return value if isinstance(value, dict) else {}


def paused_ids(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    workflows = state.get("workflows") if isinstance(state.get("workflows"), dict) else {}
    programs = state.get("programs") if isinstance(state.get("programs"), dict) else {}
    return _uuids(workflows.keys()), _uuids(programs.keys())


def is_global(state: dict[str, Any]) -> bool:
    return isinstance(state.get("global"), dict)


def covered(cur, workflow_ids: list[str], program_ids: list[str]) -> tuple[set[str], set[str]]:
    """``(workflow ids, program ids)`` covered by pauses on these workflows and programs."""
    if not workflow_ids and not program_ids:
        return set(), set()
    params = {"workflows": workflow_ids, "programs": program_ids}
    cur.execute(_TREE + "SELECT id::text AS id, 'w' AS k FROM tree UNION ALL SELECT id::text, 'p' FROM progs", params)
    rows = list(cur.fetchall())
    return ({str(r["id"]) for r in rows if r["k"] == "w"}, {str(r["id"]) for r in rows if r["k"] == "p"})


def held_job_ids(cur, rows: list[dict[str, Any]], state: dict[str, Any] | None = None) -> dict[str, str]:
    """``{job id: held reason}`` for the queued jobs a pause covers (claim leaves them queued)."""
    state = read_state(cur) if state is None else state
    queued = [r for r in rows if r.get("status") == "queued"]
    if not queued or not state:
        return {}
    if is_global(state):
        return {str(r["id"]): GLOBAL_HELD_REASON for r in queued}
    workflows, programs = covered(cur, *paused_ids(state))
    if not workflows and not programs:
        return {}
    return {
        str(r["id"]): PAUSED_HELD_REASON
        for r in queued
        if str(r.get("workflow_id") or "") in workflows or str(r.get("program") or "") in programs
    }


def hold_paused_task(conn, body: dict[str, Any]) -> dict[str, Any]:
    task_id = _uuid(body.get("id"))
    hold = body.get("hold") if isinstance(body.get("hold"), dict) else {}
    actor = str(body.get("actor") or "workflow-runner")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_tasks
               SET status = 'blocked',
                   error = %s,
                   wait_token = %s,
                   expected_event_type = %s,
                   escalate_at = NULL,
                   expires_at = NULL,
                   escalated_at = NULL,
                   locked_by = NULL,
                   lock_expires_at = NULL,
                   input = COALESCE(input, '{}'::jsonb) || jsonb_build_object(%s::text, %s::jsonb)
             WHERE id = %s AND NOT (status = ANY(%s))
            RETURNING workflow_id
            """,
            (WAIT_ERROR, f"pause:{uuid.uuid4()}", EVENT_TYPE, HOLD_KEY, json.dumps(hold), task_id, list(_OPEN)),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise ValueError("workflow task not found or already finished")
        _audit(cur, "workflow_task_blocked", "Workflow task held until its pause ends",
               {"task_id": task_id, "workflow_id": str(row["workflow_id"]), "hold": hold}, actor)
    conn.commit()
    return {"id": task_id, "status": "blocked", "error": WAIT_ERROR}


def _held_rows(cur, tenant_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT t.id, t.workflow_id, t.workflow_type, t.step_name, t.input, t.created_at, t.updated_at,
               workflow_program_id(t.workflow_id)::text AS program_id
          FROM workflow_tasks t
         WHERE t.tenant_id = %s AND t.status = 'blocked' AND t.error = %s
         ORDER BY t.updated_at
         LIMIT 500
        """,
        (tenant_id, WAIT_ERROR),
    )
    return list(cur.fetchall())


def _hold(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("input")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}
    hold = (data or {}).get(HOLD_KEY) if isinstance(data, dict) else None
    return hold if isinstance(hold, dict) else {}


def _public(row: dict[str, Any], still: bool) -> dict[str, Any]:
    return {
        "task_id": str(row["id"]),
        "workflow_id": str(row["workflow_id"]),
        "workflow_type": row.get("workflow_type"),
        "step_name": row.get("step_name"),
        "program_id": row.get("program_id"),
        "hold": _hold(row),
        "since": row.get("updated_at"),
        "still_paused": still,
    }


def _still_paused(cur, rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], bool]]:
    state = read_state(cur)
    workflows, programs = covered(cur, *paused_ids(state))
    return [(r, str(r["workflow_id"]) in workflows or str(r.get("program_id") or "") in programs) for r in rows]


def list_paused_tasks(conn, body: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(body.get("tenant_id") or "default")
    with conn.cursor() as cur:
        rows = _held_rows(cur, tenant_id)
        marked = _still_paused(cur, rows) if rows else []
    conn.commit()
    return {"tasks": [_public(row, still) for row, still in marked]}


def release_paused_tasks(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Pending again: every held task of the tenant that no pause covers any more."""
    tenant_id = str(body.get("tenant_id") or "default")
    actor = str(body.get("actor") or "orchestrator")
    released: list[dict[str, Any]] = []
    still = 0
    with conn.cursor() as cur:
        rows = _held_rows(cur, tenant_id)
        for row, paused in _still_paused(cur, rows) if rows else []:
            if paused:
                still += 1
                continue
            cur.execute(
                """
                UPDATE workflow_tasks
                   SET status = 'pending',
                       error = NULL,
                       wait_token = NULL,
                       expected_event_type = NULL,
                       locked_by = NULL,
                       lock_expires_at = NULL,
                       attempt = GREATEST(attempt - 1, 0),
                       next_attempt_at = now(),
                       input = COALESCE(input, '{}'::jsonb) || jsonb_build_object(
                           %s::text, NULL,
                           %s::text, COALESCE(input -> %s, '{}'::jsonb)
                                     || jsonb_build_object('resumed_at', now(), 'resumed_by', %s::text))
                 WHERE id = %s AND status = 'blocked' AND error = %s
                """,
                (HOLD_KEY, LAST_KEY, HOLD_KEY, actor, row["id"], WAIT_ERROR),
            )
            if getattr(cur, "rowcount", 1) == 0:
                continue
            _audit(cur, "workflow_task_external_resumed", f"Workflow task resumed via external event: {EVENT_TYPE}",
                   {"task_id": str(row["id"]), "workflow_id": str(row["workflow_id"]), "event_type": EVENT_TYPE}, actor)
            released.append({"task_id": str(row["id"]), "workflow_id": str(row["workflow_id"]),
                             "step_name": row.get("step_name"), "kind": _hold(row).get("kind")})
    conn.commit()
    return {"released": released, "still_held": still}


def shift_wait_deadlines(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Move open wait and gate deadlines in the scope on by ``seconds`` (the time it was paused)."""
    try:
        seconds = int(body.get("seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("seconds must be an integer") from exc
    workflows, programs = _uuids(body.get("workflow_ids")), _uuids(body.get("program_ids"))
    if seconds <= 0 or (not workflows and not programs):
        return {"shifted": 0}
    tenant_id = str(body.get("tenant_id") or "default")
    with conn.cursor() as cur:
        cur.execute(
            _TREE
            + """
            UPDATE workflow_tasks
               SET escalate_at = CASE WHEN escalated_at IS NULL THEN escalate_at + make_interval(secs => %(seconds)s)
                                      ELSE escalate_at END,
                   expires_at = expires_at + make_interval(secs => %(seconds)s)
             WHERE tenant_id = %(tenant)s
               AND workflow_id IN (SELECT id FROM tree)
               AND status IN ('pending', 'blocked')
               AND (escalate_at IS NOT NULL OR expires_at IS NOT NULL)
            """,
            {"workflows": workflows, "programs": programs, "seconds": seconds, "tenant": tenant_id},
        )
        shifted = int(getattr(cur, "rowcount", 0) or 0)
    conn.commit()
    return {"shifted": shifted}
