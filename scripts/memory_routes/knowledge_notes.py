"""Reviewed knowledge notes, org-wide and per project.

Active notes are what a run is allowed to see. Proposals stay out of that
list until someone approves them. Writes are compare-and-set on ``version``:
a stale ``expected_version`` raises ``NoteConflict`` and changes nothing.

``NOTE_LEVELS`` is the only ordering of scopes. A later team level is another
entry in that tuple plus a migration that extends the scope check.
"""

from __future__ import annotations

import json
import os
import uuid
from fnmatch import fnmatch
from typing import Any

from psycopg.types.json import Jsonb

NOTE_LEVELS = ("org", "project")
SCOPES = NOTE_LEVELS
KINDS = ("avoid", "howto")
STATUSES = ("proposed", "active", "retired", "rejected")
SOURCES = ("manual", "review", "agent")
PROPOSAL_ACTIONS = ("add", "edit", "retire", "promote")

MAX_TITLE = 120
MAX_BODY = 300
MAX_WHY = 500
MAX_APPLIES = 20
MAX_EVIDENCE = 20

DEFAULT_MAX_CHARS = 3000
DEFAULT_MAX_NOTES = 15
DEFAULT_ORG_SHARE = 1 / 3


class NoteConflict(Exception):
    """The note changed since the caller read it."""

    def __init__(self, note: dict[str, Any]):
        super().__init__("version conflict")
        self.note = note


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _clean(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return text


def _choice(value: Any, field: str, allowed: tuple[str, ...], default: str | None = None) -> str:
    text = str(value or default or "").strip()
    if text not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(allowed)}")
    return text


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _string_list(value: Any, field: str, *, limit: int, item_limit: int) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    items = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > item_limit:
            raise ValueError(f"{field} entries must be at most {item_limit} characters")
        items.append(text)
    if len(items) > limit:
        raise ValueError(f"{field} has too many entries")
    return items


def _evidence(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("evidence must be a list") from exc
    if not isinstance(value, list):
        raise ValueError("evidence must be a list")
    if len(value) > MAX_EVIDENCE:
        raise ValueError("evidence has too many entries")
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("evidence entries must be objects")
        cleaned.append(
            {
                "type": _clean(item.get("type"), "evidence.type", 40),
                "ref": _clean(item.get("ref"), "evidence.ref", 200),
                "url": _clean(item.get("url"), "evidence.url", 500),
                "excerpt": _clean(item.get("excerpt"), "evidence.excerpt", 300),
            }
        )
    return cleaned


def _public(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") or []
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    applies = list(row.get("applies_to") or [])
    return {
        "id": str(row["id"]),
        "tenant_id": row.get("tenant_id") or "default",
        "scope": row["scope"],
        "project_id": str(row["project_id"]) if row.get("project_id") else None,
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "why": row.get("why") or "",
        "applies_to": applies,
        "exception_to": str(row["exception_to"]) if row.get("exception_to") else None,
        "status": row["status"],
        "version": int(row["version"]),
        "evidence": evidence,
        "source": row.get("source") or "manual",
        "proposal_action": row.get("proposal_action") or "add",
        "revision_of": str(row["revision_of"]) if row.get("revision_of") else None,
        "base_version": int(row["base_version"]) if row.get("base_version") is not None else None,
        "promoted_from": str(row["promoted_from"]) if row.get("promoted_from") else None,
        "proposed_by": row.get("proposed_by") or "",
        "approved_by": row.get("approved_by"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "last_used_at": _iso(row.get("last_used_at")),
        "use_count": int(row.get("use_count") or 0),
    }


def _context_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note["id"],
        "scope": note["scope"],
        "project_id": note.get("project_id"),
        "kind": note["kind"],
        "title": note["title"],
        "body": note["body"],
        "why": note.get("why") or "",
        "applies_to": list(note.get("applies_to") or []),
    }


def note_chars(note: dict[str, Any]) -> int:
    return len(note.get("title") or "") + len(note.get("body") or "") + len(note.get("why") or "")


def _path_match(path: str, pattern: str) -> bool:
    if fnmatch(path, pattern):
        return True
    name = path.rsplit("/", 1)[-1]
    return fnmatch(name, pattern)


def _match_score(note: dict[str, Any], stage: str, paths: list[str]) -> int:
    applies = [str(item) for item in (note.get("applies_to") or []) if str(item).strip()]
    if stage and stage in applies:
        return 2
    if paths and any(_path_match(path, pattern) for pattern in applies for path in paths):
        return 1
    return 0


def rank_notes(notes: list[dict[str, Any]], *, stage: str = "", paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Specific notes first, then avoid before howto, then the newest."""
    found = list(paths or [])

    def key(note: dict[str, Any]) -> tuple[int, int, str]:
        kind_rank = 1 if note.get("kind") == "avoid" else 0
        return (_match_score(note, stage, found), kind_rank, str(note.get("updated_at") or ""))

    return sorted(notes, key=key, reverse=True)


def _take(notes: list[dict[str, Any]], cap_chars: int, cap_notes: int) -> tuple[list[dict[str, Any]], int, int]:
    if cap_chars <= 0 or cap_notes <= 0:
        return [], 0, 0
    picked: list[dict[str, Any]] = []
    used = 0
    for note in notes:
        if len(picked) >= cap_notes:
            break
        cost = note_chars(note)
        if used + cost > cap_chars:
            continue
        picked.append(note)
        used += cost
    return picked, cap_chars - used, cap_notes - len(picked)


def context_limits(query: dict[str, Any] | None = None) -> dict[str, Any]:
    """Budget for one run. Query values can only shrink the configured ceiling."""
    max_chars = int(os.environ.get("KNOWLEDGE_NOTES_MAX_CHARS", str(DEFAULT_MAX_CHARS)))
    max_notes = int(os.environ.get("KNOWLEDGE_NOTES_MAX_NOTES", str(DEFAULT_MAX_NOTES)))
    share = float(os.environ.get("KNOWLEDGE_NOTES_ORG_SHARE", str(DEFAULT_ORG_SHARE)))
    query = query or {}
    if query.get("max_chars"):
        max_chars = min(max_chars, int(query["max_chars"]))
    if query.get("max_notes"):
        max_notes = min(max_notes, int(query["max_notes"]))
    if query.get("org_share") not in (None, ""):
        share = float(query["org_share"])
    share = min(1.0, max(0.0, share))
    shares = {"org": share}
    return {"max_chars": max(1, max_chars), "max_notes": max(1, max_notes), "shares": shares}


def select_for_context(
    grouped: dict[str, list[dict[str, Any]]],
    *,
    stage: str = "",
    paths: list[str] | None = None,
    limits: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Merge levels in ``NOTE_LEVELS`` order, keeping each level's reserved share.

    An active project note with ``exception_to`` hides that org note for this
    project. Unused share from an earlier level is offered to later levels,
    then a second pass lets earlier levels use whatever is still left, so a
    run with no project still receives the org notes that fit the full budget.
    """
    limits = limits or context_limits()
    ranked = {
        level: rank_notes(list(grouped.get(level) or []), stage=stage, paths=paths) for level in NOTE_LEVELS
    }
    hidden = {
        str(note.get("exception_to"))
        for note in ranked.get("project") or []
        if note.get("exception_to")
    }
    if "org" in ranked:
        ranked["org"] = [note for note in ranked["org"] if note["id"] not in hidden]

    max_chars = int(limits["max_chars"])
    max_notes = int(limits["max_notes"])
    shares = dict(limits.get("shares") or {})
    reserved: dict[str, tuple[int, int]] = {}
    claimed_chars = 0
    claimed_notes = 0
    for level in NOTE_LEVELS[:-1]:
        share = float(shares.get(level, 0))
        reserved[level] = (int(max_chars * share), int(max_notes * share))
        claimed_chars += reserved[level][0]
        claimed_notes += reserved[level][1]
    reserved[NOTE_LEVELS[-1]] = (max_chars - claimed_chars, max_notes - claimed_notes)

    taken: dict[str, list[dict[str, Any]]] = {}
    leftover_chars = 0
    leftover_notes = 0
    for level in NOTE_LEVELS:
        cap_chars, cap_notes = reserved[level]
        picked, unused_chars, unused_notes = _take(ranked[level], cap_chars, cap_notes)
        taken[level] = picked
        leftover_chars += unused_chars
        leftover_notes += unused_notes
    if leftover_chars > 0 and leftover_notes > 0:
        for level in NOTE_LEVELS:
            seen = {note["id"] for note in taken[level]}
            rest = [note for note in ranked[level] if note["id"] not in seen]
            extra, leftover_chars, leftover_notes = _take(rest, leftover_chars, leftover_notes)
            taken[level].extend(extra)
            if leftover_chars <= 0 or leftover_notes <= 0:
                break
    chosen: list[dict[str, Any]] = []
    for level in NOTE_LEVELS:
        chosen.extend(taken[level])
    return chosen


def _audit(cur, action: str, summary: str, details: dict[str, Any], actor: str) -> None:
    cur.execute(
        """
        INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, occurred_at)
        VALUES (uuid_generate_v4(), %s, %s, %s, %s, %s::jsonb, now())
        """,
        (str(details.get("note_id") or "knowledge"), actor, action, summary, json.dumps(details, default=str)),
    )


def _lock(cur, note_id: str) -> dict[str, Any]:
    cur.execute("SELECT * FROM knowledge_notes WHERE id = %s FOR UPDATE", (note_id,))
    row = cur.fetchone()
    if not row:
        raise LookupError("note not found")
    return row


def _insert_version(cur, note: dict[str, Any], actor: str, reason: str) -> None:
    public = _public(note)
    cur.execute(
        """
        INSERT INTO knowledge_note_versions (note_id, version, snapshot, changed_by, change_reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (public["id"], public["version"], Jsonb(public), actor, reason or ""),
    )


def _resolve_project(cur, tenant_id: str, value: Any, *, required: bool) -> str | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError("project_id is required")
        return None
    try:
        as_uuid = str(uuid.UUID(text))
    except (TypeError, ValueError):
        as_uuid = ""
    if as_uuid:
        cur.execute("SELECT id FROM projects WHERE id = %s AND tenant_id = %s", (as_uuid, tenant_id))
        row = cur.fetchone()
        if row:
            return str(row["id"])
    cur.execute("SELECT id FROM projects WHERE tenant_id = %s AND key = %s", (tenant_id, text))
    row = cur.fetchone()
    if not row:
        if required:
            raise ValueError(f"unknown project: {text}")
        return None
    return str(row["id"])


def _load_org_target(cur, tenant_id: str, exception_to: str | None) -> None:
    if not exception_to:
        return
    cur.execute(
        "SELECT id, scope, tenant_id FROM knowledge_notes WHERE id = %s",
        (exception_to,),
    )
    row = cur.fetchone()
    if not row or row["tenant_id"] != tenant_id or row["scope"] != "org":
        raise ValueError("exception_to must reference an org note in this tenant")


def get_note(conn, note_id: str) -> dict[str, Any]:
    nid = _uuid(note_id, "note_id")
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM knowledge_notes WHERE id = %s", (nid,))
        row = cur.fetchone()
    if not row:
        raise LookupError("note not found")
    return _public(row)


def list_notes(conn, query: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(query.get("tenant_id") or "default")
    where = ["tenant_id = %s"]
    params: list[Any] = [tenant_id]
    if query.get("scope"):
        where.append("scope = %s")
        params.append(_choice(query.get("scope"), "scope", SCOPES))
    if query.get("status"):
        where.append("status = %s")
        params.append(_choice(query.get("status"), "status", STATUSES))
    if query.get("proposal_action"):
        where.append("proposal_action = %s")
        params.append(_choice(query.get("proposal_action"), "proposal_action", PROPOSAL_ACTIONS))
    if query.get("source"):
        where.append("source = %s")
        params.append(_choice(query.get("source"), "source", SOURCES))
    with conn.cursor() as cur:
        if query.get("project_id"):
            project_id = _resolve_project(cur, tenant_id, query.get("project_id"), required=True)
            where.append("project_id = %s")
            params.append(project_id)
        if query.get("revision_of"):
            where.append("revision_of = %s")
            params.append(_uuid(query.get("revision_of"), "revision_of"))
        cur.execute(
            f"SELECT * FROM knowledge_notes WHERE {' AND '.join(where)} ORDER BY updated_at DESC, id",
            params,
        )
        rows = cur.fetchall()
    return {"notes": [_public(row) for row in rows]}


def _content(body: dict[str, Any], *, current: dict[str, Any] | None = None) -> dict[str, Any]:
    source = current or {}
    title = _clean(body.get("title", source.get("title")), "title", MAX_TITLE, required=True)
    text = _clean(body.get("body", source.get("body")), "body", MAX_BODY, required=True)
    why = _clean(body.get("why", source.get("why")), "why", MAX_WHY)
    kind = _choice(body.get("kind", source.get("kind")), "kind", KINDS)
    applies = _string_list(
        body.get("applies_to", source.get("applies_to")), "applies_to", limit=MAX_APPLIES, item_limit=200
    )
    return {"title": title, "body": text, "why": why, "kind": kind, "applies_to": applies}


def create_note(conn, body: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(body.get("tenant_id") or "default")
    scope = _choice(body.get("scope"), "scope", SCOPES)
    source = _choice(body.get("source") or "manual", "source", SOURCES)
    action = _choice(body.get("proposal_action") or "add", "proposal_action", PROPOSAL_ACTIONS)
    status = _choice(body.get("status") or "proposed", "status", ("proposed", "active"))
    if source in {"agent", "review"} and status == "active":
        raise ValueError("agent and review notes start as proposals")
    content = _content(body)
    evidence = _evidence(body.get("evidence"))
    actor = str(body.get("actor") or body.get("proposed_by") or "admin")
    exception_to = str(body.get("exception_to") or "").strip() or None
    revision_of = str(body.get("revision_of") or "").strip() or None
    promoted_from = str(body.get("promoted_from") or "").strip() or None
    base_version = body.get("base_version")
    if scope == "org" and (body.get("project_id") or exception_to):
        raise ValueError("org notes have no project and cannot be exceptions")
    if action == "edit" and (not revision_of or base_version in (None, "")):
        raise ValueError("an edit proposal needs revision_of and base_version")
    if action == "retire" and not revision_of:
        raise ValueError("a retire proposal needs revision_of")
    if action == "promote" and not promoted_from:
        raise ValueError("a promotion needs promoted_from")
    if action == "add":
        revision_of = None
        base_version = None
    try:
        with conn.cursor() as cur:
            project_id = None
            if scope == "project":
                project_id = _resolve_project(cur, tenant_id, body.get("project_id"), required=True)
            if exception_to:
                exception_to = _uuid(exception_to, "exception_to")
                _load_org_target(cur, tenant_id, exception_to)
            if revision_of:
                revision_of = _uuid(revision_of, "revision_of")
            if promoted_from:
                promoted_from = _uuid(promoted_from, "promoted_from")
            if base_version not in (None, ""):
                base_version = int(base_version)
            cur.execute(
                """
                INSERT INTO knowledge_notes (
                    tenant_id, scope, project_id, kind, title, body, why, applies_to,
                    exception_to, status, version, evidence, source, proposal_action,
                    revision_of, base_version, promoted_from, proposed_by, approved_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    tenant_id,
                    scope,
                    project_id,
                    content["kind"],
                    content["title"],
                    content["body"],
                    content["why"],
                    content["applies_to"],
                    exception_to,
                    status,
                    Jsonb(evidence),
                    source,
                    action,
                    revision_of,
                    base_version,
                    promoted_from,
                    actor,
                    actor if status == "active" else None,
                ),
            )
            row = cur.fetchone()
            _insert_version(cur, row, actor, str(body.get("change_reason") or "created"))
            _audit(
                cur,
                "knowledge_note_created",
                f"Knowledge note created: {content['title']}",
                {"note_id": str(row["id"]), "scope": scope, "status": status, "project_id": project_id},
                actor,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _public(row)


def update_note(conn, note_id: str, body: dict[str, Any]) -> dict[str, Any]:
    nid = _uuid(note_id, "note_id")
    if "scope" in body or "project_id" in body:
        raise ValueError("scope cannot be changed; promote the note instead")
    expected = int(body.get("expected_version"))
    actor = str(body.get("actor") or "admin")
    reason = str(body.get("change_reason") or "edited")
    try:
        with conn.cursor() as cur:
            current = _lock(cur, nid)
            if int(current["version"]) != expected:
                raise NoteConflict(_public(current))
            content = _content(body, current=_public(current))
            evidence = _evidence(body["evidence"]) if "evidence" in body else _public(current)["evidence"]
            cur.execute(
                """
                UPDATE knowledge_notes
                   SET title = %s, body = %s, why = %s, kind = %s, applies_to = %s,
                       evidence = %s, version = version + 1, updated_at = now()
                 WHERE id = %s AND version = %s
                RETURNING *
                """,
                (
                    content["title"],
                    content["body"],
                    content["why"],
                    content["kind"],
                    content["applies_to"],
                    Jsonb(evidence),
                    nid,
                    expected,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise NoteConflict(_public(current))
            _insert_version(cur, row, actor, reason)
            _audit(
                cur,
                "knowledge_note_updated",
                f"Knowledge note updated: {content['title']}",
                {"note_id": nid, "version": int(row["version"])},
                actor,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _public(row)


def _bump_status(cur, note_id: str, expected: int, status: str, actor: str, *, approve: bool) -> dict[str, Any]:
    cur.execute(
        """
        UPDATE knowledge_notes
           SET status = %s,
               version = version + 1,
               approved_by = CASE WHEN %s THEN %s ELSE approved_by END,
               updated_at = now()
         WHERE id = %s AND version = %s
        RETURNING *
        """,
        (status, approve, actor, note_id, expected),
    )
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT * FROM knowledge_notes WHERE id = %s", (note_id,))
        current = cur.fetchone()
        raise NoteConflict(_public(current) if current else {"id": note_id})
    return row


def _apply_content_to(cur, target_id: str, base_version: int, content: dict[str, Any], actor: str, reason: str) -> dict[str, Any]:
    target = _lock(cur, target_id)
    if int(target["version"]) != int(base_version):
        raise NoteConflict(_public(target))
    cur.execute(
        """
        UPDATE knowledge_notes
           SET title = %s, body = %s, why = %s, kind = %s, applies_to = %s,
               version = version + 1, updated_at = now()
         WHERE id = %s AND version = %s
        RETURNING *
        """,
        (
            content["title"],
            content["body"],
            content["why"],
            content["kind"],
            content["applies_to"],
            target_id,
            int(base_version),
        ),
    )
    row = cur.fetchone()
    if not row:
        raise NoteConflict(_public(target))
    _insert_version(cur, row, actor, reason)
    return row


def _retire(cur, note_id: str, actor: str, reason: str) -> None:
    cur.execute("SELECT * FROM knowledge_notes WHERE id = %s FOR UPDATE", (note_id,))
    row = cur.fetchone()
    if not row or row["status"] == "retired":
        return
    updated = _bump_status(cur, note_id, int(row["version"]), "retired", actor, approve=False)
    _insert_version(cur, updated, actor, reason)


def set_note_status(conn, note_id: str, body: dict[str, Any]) -> dict[str, Any]:
    nid = _uuid(note_id, "note_id")
    status = _choice(body.get("status"), "status", ("active", "retired", "rejected"))
    expected = int(body.get("expected_version"))
    actor = str(body.get("actor") or "admin")
    reason = str(body.get("change_reason") or body.get("reason") or status)
    try:
        with conn.cursor() as cur:
            current = _lock(cur, nid)
            if int(current["version"]) != expected:
                raise NoteConflict(_public(current))
            public = _public(current)
            action = public["proposal_action"]
            if status == "active" and action == "edit":
                _apply_content_to(
                    cur,
                    public["revision_of"],
                    int(public["base_version"]),
                    _content({}, current=public),
                    actor,
                    f"approved edit {nid}",
                )
                row = _bump_status(cur, nid, expected, "retired", actor, approve=True)
            elif status == "active" and action == "retire":
                _retire(cur, public["revision_of"], actor, f"approved retire {nid}")
                row = _bump_status(cur, nid, expected, "retired", actor, approve=True)
            elif status == "active" and action == "promote":
                row = _bump_status(cur, nid, expected, "active", actor, approve=True)
                if public.get("promoted_from"):
                    _retire(cur, public["promoted_from"], actor, f"promoted to {nid}")
            else:
                row = _bump_status(cur, nid, expected, status, actor, approve=status == "active")
            _insert_version(cur, row, actor, reason)
            _audit(
                cur,
                "knowledge_note_status_changed",
                f"Knowledge note {public['title']} is now {row['status']}",
                {"note_id": nid, "status": row["status"], "proposal_action": action},
                actor,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _public(row)


def list_versions(conn, note_id: str) -> dict[str, Any]:
    nid = _uuid(note_id, "note_id")
    get_note(conn, nid)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT version, snapshot, changed_by, change_reason, created_at
              FROM knowledge_note_versions
             WHERE note_id = %s
             ORDER BY version
            """,
            (nid,),
        )
        rows = cur.fetchall()
    return {
        "versions": [
            {
                "version": int(row["version"]),
                "snapshot": row["snapshot"],
                "changed_by": row["changed_by"],
                "change_reason": row["change_reason"],
                "created_at": _iso(row["created_at"]),
            }
            for row in rows
        ]
    }


def restore_note(conn, note_id: str, body: dict[str, Any]) -> dict[str, Any]:
    nid = _uuid(note_id, "note_id")
    expected = int(body.get("expected_version"))
    version = int(body.get("version"))
    actor = str(body.get("actor") or "admin")
    try:
        with conn.cursor() as cur:
            current = _lock(cur, nid)
            if int(current["version"]) != expected:
                raise NoteConflict(_public(current))
            cur.execute(
                "SELECT snapshot FROM knowledge_note_versions WHERE note_id = %s AND version = %s",
                (nid, version),
            )
            found = cur.fetchone()
            if not found:
                raise LookupError("version not found")
            snapshot = found["snapshot"] or {}
            content = _content(snapshot)
            status = _choice(snapshot.get("status") or current["status"], "status", STATUSES)
            cur.execute(
                """
                UPDATE knowledge_notes
                   SET title = %s, body = %s, why = %s, kind = %s, applies_to = %s,
                       status = %s, version = version + 1, updated_at = now()
                 WHERE id = %s AND version = %s
                RETURNING *
                """,
                (
                    content["title"],
                    content["body"],
                    content["why"],
                    content["kind"],
                    content["applies_to"],
                    status,
                    nid,
                    expected,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise NoteConflict(_public(current))
            _insert_version(cur, row, actor, f"restored version {version}")
            _audit(
                cur,
                "knowledge_note_updated",
                f"Knowledge note restored to version {version}",
                {"note_id": nid, "restored_version": version, "version": int(row["version"])},
                actor,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _public(row)


def promote_note(conn, note_id: str, body: dict[str, Any]) -> dict[str, Any]:
    source = get_note(conn, note_id)
    if source["scope"] != "project":
        raise ValueError("only a project note can be promoted")
    if source["status"] not in {"active", "proposed"}:
        raise ValueError("only an active or proposed project note can be promoted")
    tenant_id = source["tenant_id"]
    actor = str(body.get("actor") or "admin")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM knowledge_notes
                 WHERE tenant_id = %s AND scope = 'org' AND status = 'proposed'
                   AND promoted_from = %s
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (tenant_id, source["id"]),
            )
            existing = cur.fetchone()
        if existing:
            return _public(existing)
    except Exception:
        conn.rollback()
        raise
    evidence = list(source.get("evidence") or [])
    evidence.append({"type": "promoted_from", "ref": source["id"], "url": "", "excerpt": source["title"]})
    return create_note(
        conn,
        {
            "tenant_id": tenant_id,
            "scope": "org",
            "kind": source["kind"],
            "title": source["title"],
            "body": source["body"],
            "why": source["why"],
            "applies_to": source["applies_to"],
            "evidence": evidence,
            "source": str(body.get("source") or "review"),
            "proposal_action": "promote",
            "promoted_from": source["id"],
            "status": "proposed",
            "actor": actor,
            "change_reason": "promoted from project note",
        },
    )


def _split_paths(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def notes_context(conn, query: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(query.get("tenant_id") or "default")
    stage = str(query.get("stage") or "").strip()
    paths = _split_paths(query.get("paths"))
    limits = context_limits(query)
    try:
        with conn.cursor() as cur:
            project_id = _resolve_project(cur, tenant_id, query.get("project_id"), required=False)
            cur.execute(
                """
                SELECT * FROM knowledge_notes
                 WHERE tenant_id = %s AND status = 'active' AND scope = 'org'
                """,
                (tenant_id,),
            )
            org_rows = list(cur.fetchall())
            project_rows: list[dict[str, Any]] = []
            if project_id:
                cur.execute(
                    """
                    SELECT * FROM knowledge_notes
                     WHERE tenant_id = %s AND status = 'active'
                       AND scope = 'project' AND project_id = %s
                    """,
                    (tenant_id, project_id),
                )
                project_rows = list(cur.fetchall())
        grouped = {
            "org": [_public(row) for row in org_rows],
            "project": [_public(row) for row in project_rows],
        }
        chosen = select_for_context(grouped, stage=stage, paths=paths, limits=limits)
        considered = len(grouped["org"]) + len(grouped["project"])
        if chosen:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE knowledge_notes
                       SET use_count = use_count + 1, last_used_at = now()
                     WHERE id = ANY(%s::uuid[])
                    """,
                    ([note["id"] for note in chosen],),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"notes": [_context_note(note) for note in chosen], "omitted": considered - len(chosen)}


def list_programs_pending_review(conn, query: dict[str, Any]) -> dict[str, Any]:
    """Finished programs with no review mark, updated after ``since`` when given."""
    tenant_id = str(query.get("tenant_id") or "default")
    limit = min(int(query.get("limit") or 20), 50)
    since = str(query.get("since") or "").strip()
    where = [
        "tenant_id = %s",
        "status IN ('succeeded', 'failed', 'cancelled')",
        "(metadata->>'notes_reviewed_at') IS NULL",
        "(metadata->>'notes_review_started_at') IS NULL",
    ]
    params: list[Any] = [tenant_id]
    if since:
        where.append("updated_at > %s")
        params.append(since)
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, tenant_id, project_id, project_ref, status, program_type, metadata, updated_at
              FROM delivery_programs
             WHERE {' AND '.join(where)}
             ORDER BY updated_at ASC
             LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    programs = []
    for row in rows:
        programs.append(
            {
                "id": str(row["id"]),
                "tenant_id": row.get("tenant_id"),
                "project_id": row.get("project_id"),
                "project_ref": str(row["project_ref"]) if row.get("project_ref") else None,
                "status": row.get("status"),
                "program_type": row.get("program_type"),
                "metadata": row.get("metadata") or {},
                "updated_at": _iso(row.get("updated_at")),
            }
        )
    return {"programs": programs}


_REVIEW_STAMPS = {"notes_reviewed_at", "notes_review_started_at"}


def mark_program_reviewed(conn, program_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    pid = _uuid(program_id, "program_id")
    body = body or {}
    actor = str(body.get("actor") or "review")
    key = str(body.get("key") or "notes_reviewed_at")
    if key not in _REVIEW_STAMPS:
        raise ValueError("unsupported review stamp")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, metadata FROM delivery_programs WHERE id = %s FOR UPDATE", (pid,))
            row = cur.fetchone()
            if not row:
                raise LookupError("program not found")
            metadata = row.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if not isinstance(metadata, dict):
                metadata = {}
            cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ts")
            stamp = cur.fetchone()["ts"]
            metadata[key] = stamp
            cur.execute(
                "UPDATE delivery_programs SET metadata = %s, updated_at = now() WHERE id = %s",
                (Jsonb(metadata), pid),
            )
            _audit(
                cur,
                "knowledge_note_status_changed",
                "Program note review recorded",
                {"note_id": pid, "program_id": pid, key: stamp},
                actor,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"program_id": pid, key: stamp}
