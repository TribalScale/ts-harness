"""Harness self-issues (migration 057): the failure classes the harness files against its own repository.

Routes (memory API, under ``/harness-issues``):

* ``GET  /harness-issues``                 list (``tenant_id``, ``limit``) with the self-fix counts;
* ``POST /harness-issues/occurrences``     record one occurrence of a failure class (upsert by
                                           fingerprint; the same occurrence ``key`` counts once);
* ``PATCH /harness-issues/<id>``           update the issue and self-fix fields.

The runtime sanitises every text before it gets here (ts_harness.improvement.sanitize); these
routes only cap lengths and keep the counts. :func:`merge_occurrence` is the pure merge both the
route and the tests use.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

MAX_SEEN_KEYS = 200
MAX_REFS = 20
OPEN_FIX_STATUSES = ("started", "pr_open")
FIX_STATUSES = ("none", "not_fixable", "needs_human", "started", "pr_open", "merged", "closed", "failed")
SOURCES = ("arbiter", "ops_alert")
KINDS = ("code", "configuration", "operations", "unknown")
TEXT_LIMITS = {"title": 200, "signature": 500, "diagnosis": 1500, "stack": 4000, "component": 300}
UPDATABLE = {
    "issue_repo": 200,
    "issue_url": 500,
    "issue_state": 20,
    "fix_status": 20,
    "fix_reason": 600,
    "fix_workflow_id": 80,
    "fix_pr_url": 500,
    "fix_pr_head": 80,
    "last_error": 600,
}
_COLUMNS = (
    "id::text AS id, tenant_id, fingerprint, source, kind, component, title, signature, diagnosis, stack, files, "
    "occurrences, reported_occurrences, refs, first_seen, last_seen, issue_repo, issue_number, issue_url, "
    "issue_state, last_reported_at, fix_status, fix_reason, fix_workflow_id, fix_pr_url, fix_pr_head, "
    "fix_started_at, fix_charged_usd, last_error, created_at, updated_at"
)


def _tenant(body: dict[str, Any]) -> str:
    return str(body.get("tenant_id") or "default")


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if out.get("fix_charged_usd") is not None:
        out["fix_charged_usd"] = round(float(out["fix_charged_usd"]), 4)
    return out


def merge_occurrence(existing: dict[str, Any] | None, body: dict[str, Any], now: str) -> tuple[dict[str, Any], bool]:
    """The row after one occurrence, and whether the occurrence was new.

    ``body``: ``fingerprint``, ``source``, ``kind``, ``component``, ``title``, ``signature``,
    ``diagnosis``, ``stack``, ``files``, ``key`` (what makes two sightings the same occurrence: a
    task's stop, a lost job) and ``ref`` (internal ids). The first sighting's texts are kept; a
    later sighting only adds its key, ref and ``last_seen``.
    """
    fingerprint = _text(body.get("fingerprint"), 64)
    if len(fingerprint) < 8:
        raise ValueError("fingerprint is required (8 to 64 characters)")
    source = str(body.get("source") or "")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    kind = str(body.get("kind") or "unknown")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    key = _text(body.get("key"), 200)
    if not key:
        raise ValueError("key is required")
    ref = body.get("ref") if isinstance(body.get("ref"), dict) else {}
    ref = {str(k)[:40]: str(v)[:120] for k, v in ref.items() if v not in (None, "")}
    if existing is None:
        title = _text(body.get("title"), TEXT_LIMITS["title"])
        if not title:
            raise ValueError("title is required")
        row = {
            "fingerprint": fingerprint,
            "source": source,
            "kind": kind,
            "component": _text(body.get("component"), TEXT_LIMITS["component"]),
            "title": title,
            "signature": _text(body.get("signature"), TEXT_LIMITS["signature"]),
            "diagnosis": _text(body.get("diagnosis"), TEXT_LIMITS["diagnosis"]),
            "stack": _text(body.get("stack"), TEXT_LIMITS["stack"]),
            "files": [str(f)[:300] for f in (body.get("files") or []) if str(f or "").strip()][:20],
            "occurrences": 1,
            "seen_keys": [key],
            "refs": [ref] if ref else [],
            "first_seen": now,
            "last_seen": now,
        }
        return row, True
    row = dict(existing)
    seen = list(row.get("seen_keys") or [])
    if key in seen:
        return row, False
    row["seen_keys"] = (seen + [key])[-MAX_SEEN_KEYS:]
    row["occurrences"] = int(row.get("occurrences") or 0) + 1
    if ref:
        row["refs"] = (list(row.get("refs") or []) + [ref])[-MAX_REFS:]
    row["last_seen"] = now
    return row, True


def record_occurrence(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Upsert one occurrence; ``{"issue": row, "new_occurrence": bool, "created": bool}``."""
    tenant = _tenant(body)
    fingerprint = _text(body.get("fingerprint"), 64)
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text AS id, occurrences, seen_keys, refs FROM harness_self_issues "
            "WHERE tenant_id = %s AND fingerprint = %s FOR UPDATE",
            (tenant, fingerprint),
        )
        existing = cur.fetchone()
        row, new = merge_occurrence(dict(existing) if existing else None, body, now)
        if existing is None:
            cur.execute(
                """
                INSERT INTO harness_self_issues (tenant_id, fingerprint, source, kind, component, title, signature,
                                                 diagnosis, stack, files, occurrences, seen_keys, refs)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 1, %s::jsonb, %s::jsonb)
                ON CONFLICT (tenant_id, fingerprint) DO NOTHING
                """,
                (tenant, row["fingerprint"], row["source"], row["kind"], row["component"], row["title"],
                 row["signature"], row["diagnosis"], row["stack"], json.dumps(row["files"]),
                 json.dumps(row["seen_keys"]), json.dumps(row["refs"])),
            )
        elif new:
            cur.execute(
                "UPDATE harness_self_issues SET occurrences = %s, seen_keys = %s::jsonb, refs = %s::jsonb, "
                "last_seen = now(), updated_at = now() WHERE id = %s",
                (row["occurrences"], json.dumps(row["seen_keys"]), json.dumps(row["refs"]), existing["id"]),
            )
        cur.execute(f"SELECT {_COLUMNS} FROM harness_self_issues WHERE tenant_id = %s AND fingerprint = %s",
                    (tenant, fingerprint))
        stored = cur.fetchone()
    conn.commit()
    return {"issue": _jsonable(stored), "new_occurrence": new, "created": existing is None}


def list_issues(conn, params: dict[str, Any]) -> dict[str, Any]:
    """The tenant's failure classes, most recently seen first, with the self-fix counts the rate limits use."""
    tenant = _tenant(params)
    try:
        limit = max(1, min(200, int(params.get("limit") or 50)))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be a number") from exc
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM harness_self_issues WHERE tenant_id = %s ORDER BY last_seen DESC LIMIT %s",
            (tenant, limit),
        )
        issues = [_jsonable(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT count(*) FILTER (WHERE fix_status = ANY(%s)) AS open_fixes, "
            "count(*) FILTER (WHERE fix_started_at >= now() - interval '7 days') AS fixes_last_7_days "
            "FROM harness_self_issues WHERE tenant_id = %s",
            (list(OPEN_FIX_STATUSES), tenant),
        )
        counts = cur.fetchone()
    return {
        "tenant_id": tenant,
        "issues": issues,
        "open_fixes": int(counts["open_fixes"] or 0),
        "fixes_last_7_days": int(counts["fixes_last_7_days"] or 0),
    }


def update_issue(conn, issue_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Set the issue and self-fix fields named in ``body``.

    Also: ``issue_number``, ``reported_occurrences`` (numbers), ``reported`` (true: stamp
    ``last_reported_at``), ``fix_started`` (true: stamp ``fix_started_at``; refused when a fix was
    already started, so an issue gets at most one self-fix) and ``charge_usd`` (added to
    ``fix_charged_usd``).
    """
    try:
        issue_id = str(uuid.UUID(str(issue_id)))
    except ValueError as exc:
        raise LookupError(f"harness issue {issue_id} not found") from exc
    sets: list[str] = []
    values: list[Any] = []
    for field, limit in UPDATABLE.items():
        if field in body:
            value = _text(body.get(field), limit)
            if field == "fix_status" and value not in FIX_STATUSES:
                raise ValueError(f"fix_status must be one of {', '.join(FIX_STATUSES)}")
            sets.append(f"{field} = %s")
            values.append(value or (None if field.startswith(("issue_", "fix_pr", "fix_workflow")) else ""))
    for field in ("issue_number", "reported_occurrences"):
        if field in body:
            try:
                number = int(body[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} must be a whole number") from exc
            if number < 0:
                raise ValueError(f"{field} must be zero or more")
            sets.append(f"{field} = %s")
            values.append(number)
    if body.get("reported"):
        sets.append("last_reported_at = now()")
    if body.get("fix_started"):
        sets.append("fix_started_at = now()")
    if body.get("charge_usd") not in (None, ""):
        try:
            charge = round(float(body["charge_usd"]), 4)
        except (TypeError, ValueError) as exc:
            raise ValueError("charge_usd must be a number") from exc
        if charge < 0:
            raise ValueError("charge_usd must be zero or more")
        sets.append("fix_charged_usd = fix_charged_usd + %s")
        values.append(charge)
    if not sets:
        raise ValueError("nothing to update")
    guard = " AND fix_started_at IS NULL" if body.get("fix_started") else ""
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE harness_self_issues SET {', '.join(sets)}, updated_at = now() WHERE id = %s{guard} "
            f"RETURNING {_COLUMNS}",
            (*values, issue_id),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT fix_started_at FROM harness_self_issues WHERE id = %s", (issue_id,))
            found = cur.fetchone()
            conn.rollback()
            if found is None:
                raise LookupError(f"harness issue {issue_id} not found")
            raise ValueError("a self-fix was already started for this issue")
    conn.commit()
    return {"issue": _jsonable(row)}
