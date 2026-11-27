"""File events of coding runs (the console's live feed): store what workers send, list and read them.

A worker posts batches with its own job token (``POST /coding-jobs/<id>/events``). The runtime
reads a run's events, the recent events of every running job, and one event with the file's
history in that run. Access to a program's events is checked by the runtime, which only asks for
programs the person may read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

KINDS = {"created", "edited", "deleted", "renamed", "restored", "bulk"}
MAX_BATCH = 200
MAX_EXCERPT_CHARS = 40_000
MAX_PATH = 1000
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
_FIELDS = (
    "e.id, e.job_id, e.happened_at, e.kind, e.path, e.from_path, e.paths, e.file_count, e.bulk_kind, "
    "e.additions, e.deletions, e.step, e.actor, e.truncated, e.binary_file, e.masked"
)
_JOB_FIELDS = "j.work_order_id, j.program_id, j.workflow_id, j.status AS job_status, j.tenant_id"


def _uuid(value: Any) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("job_id must be a UUID") from exc


def _int(value: Any, default: int = 0, *, low: int = 0, high: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    number = max(low, number)
    return min(high, number) if high is not None else number


def _when(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc)


def _row(row: dict[str, Any], *, excerpt: bool = False) -> dict[str, Any]:
    out = {
        "id": int(row["id"]),
        "job_id": str(row["job_id"]),
        "at": row["happened_at"].isoformat() if row.get("happened_at") else None,
        "kind": row["kind"],
        "path": row["path"],
        "from_path": row.get("from_path"),
        "paths": row.get("paths") or [],
        "file_count": int(row.get("file_count") or 1),
        "bulk_kind": row.get("bulk_kind"),
        "additions": int(row.get("additions") or 0),
        "deletions": int(row.get("deletions") or 0),
        "step": row.get("step") or "",
        "actor": row.get("actor") or "agent",
        "truncated": bool(row.get("truncated")),
        "binary": bool(row.get("binary_file")),
        "masked": bool(row.get("masked")),
    }
    for key in ("work_order_id", "program_id", "workflow_id", "job_status", "tenant_id"):
        if key in row:
            out[key] = str(row[key]) if row[key] is not None else None
    if excerpt:
        out["excerpt"] = row.get("excerpt") or ""
    return out


def record_events(conn, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Store a worker's batch; events of a job that is not running are refused."""
    jid = _uuid(job_id)
    events = body.get("events") if isinstance(body.get("events"), list) else []
    rows = []
    for event in events[:MAX_BATCH]:
        if not isinstance(event, dict) or str(event.get("kind")) not in KINDS or not str(event.get("path") or ""):
            continue
        paths = event.get("paths") if isinstance(event.get("paths"), list) else None
        rows.append(
            (
                jid,
                _when(event.get("at")),
                str(event["kind"]),
                str(event["path"])[:MAX_PATH],
                str(event.get("from_path") or "")[:MAX_PATH] or None,
                json.dumps([str(p)[:MAX_PATH] for p in paths][:50]) if paths else None,
                _int(event.get("count"), 1, low=1),
                str(event.get("bulk_kind") or "") or None,
                _int(event.get("additions")),
                _int(event.get("deletions")),
                str(event.get("step") or "")[:200],
                "harness" if event.get("actor") == "harness" else "agent",
                str(event.get("excerpt") or "")[:MAX_EXCERPT_CHARS],
                bool(event.get("truncated")),
                bool(event.get("binary")),
                bool(event.get("masked")),
            )
        )
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM coding_jobs WHERE id = %s", (jid,))
        job = cur.fetchone()
        if not job:
            raise LookupError("coding job not found")
        if job["status"] not in ("starting", "running") or not rows:
            return {"stored": 0}
        cur.executemany(
            """
            INSERT INTO coding_job_events
                (job_id, happened_at, kind, path, from_path, paths, file_count, bulk_kind, additions,
                 deletions, step, actor, excerpt, truncated, binary_file, masked)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    conn.commit()
    return {"stored": len(rows)}


def list_job_events(conn, job_id: str, query: dict[str, Any]) -> dict[str, Any]:
    """A run's events after ``after`` (an event id), oldest first."""
    jid = _uuid(job_id)
    after = _int(query.get("after"))
    limit = _int(query.get("limit"), DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_FIELDS}, {_JOB_FIELDS} FROM coding_job_events e JOIN coding_jobs j ON j.id = e.job_id
                WHERE e.job_id = %s AND e.id > %s ORDER BY e.id LIMIT %s""",
            (jid, after, limit),
        )
        rows = cur.fetchall()
    events = [_row(row) for row in rows]
    return {"events": events, "next_after": events[-1]["id"] if events else after}


def list_recent_events(conn, query: dict[str, Any]) -> dict[str, Any]:
    """The newest events across jobs (all, or only running jobs), newest first, for the Live page.

    ``program_ids`` (comma separated) limits them to programs the caller may read; ``tenant_id``
    to one tenant.
    """
    after = _int(query.get("after"))
    limit = _int(query.get("limit"), DEFAULT_LIMIT, low=1, high=MAX_LIMIT)
    where, params = ["e.id > %s"], [after]
    if str(query.get("active") or "") in {"1", "true"}:
        where.append("j.status IN ('starting', 'running')")
    if query.get("tenant_id"):
        where.append("j.tenant_id = %s")
        params.append(str(query["tenant_id"]))
    if query.get("program_ids") is not None:
        ids = [_uuid(p) for p in str(query["program_ids"]).split(",") if p.strip()]
        if not ids:
            return {"events": [], "next_after": after}
        where.append("j.program_id = ANY(%s::uuid[])")
        params.append(ids)
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_FIELDS}, {_JOB_FIELDS} FROM coding_job_events e JOIN coding_jobs j ON j.id = e.job_id
                WHERE {" AND ".join(where)} ORDER BY e.id DESC LIMIT %s""",
            params,
        )
        rows = cur.fetchall()
    events = [_row(row) for row in rows]
    return {"events": events, "next_after": max([e["id"] for e in events], default=after)}


def get_event(conn, event_id: str) -> dict[str, Any]:
    """One event with its excerpt, and every event on the same file in the same run."""
    eid = _int(event_id, -1)
    if eid < 1:
        raise ValueError("event id must be a positive integer")
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_FIELDS}, e.excerpt, {_JOB_FIELDS} FROM coding_job_events e
                JOIN coding_jobs j ON j.id = e.job_id WHERE e.id = %s""",
            (eid,),
        )
        row = cur.fetchone()
        if not row:
            raise LookupError("event not found")
        cur.execute(
            f"""SELECT {_FIELDS} FROM coding_job_events e
                WHERE e.job_id = %s AND (e.path = %s OR e.from_path = %s OR e.paths ? %s) ORDER BY e.id""",
            (row["job_id"], row["path"], row["path"], row["path"]),
        )
        history = cur.fetchall()
        cur.execute("SELECT result FROM coding_jobs WHERE id = %s", (row["job_id"],))
        job = cur.fetchone() or {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    failed = [
        {"check": check.get("check"), "command": check.get("command"), "output_tail": str(check.get("output_tail") or "")[-1500:]}
        for check in verification.get("checks") or []
        if isinstance(check, dict) and check.get("exit_code") not in (0, None) and row["path"] in str(check.get("output_tail") or "")
    ]
    return {
        "event": _row(row, excerpt=True),
        "history": [_row(item) for item in history],
        "failed_checks": failed,
        "pr_url": result.get("pr_url") or None,
        "branch": result.get("branch") or None,
    }

