"""Managed skill index: drafts, approvals and the one live version per skill.

File content lives in object storage. A draft row points at a mutable draft
prefix; approval points the new live row at an immutable version prefix and
supersedes the previous live row in the same transaction.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from memory_routes.shared_state import bump_generation

SKILLS_NAMESPACE = "skills"
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PREFIX_RE = re.compile(r"^skills/[A-Za-z0-9._/-]+/$")
_STATUSES = ("draft", "active", "superseded", "rejected", "approved")
_COLUMNS = (
    "id, tenant_id, skill_id, version, category, leaf, layout, status, object_prefix, files, content_sha, "
    "source, revision_of, draft_id, created_by, created_at, updated_at, decided_by, decided_at, reason"
)


class SkillIndexConflict(Exception):
    """The request breaks a catalog rule (duplicate id, version not bumped, draft already decided)."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = str(version or "").strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        raise SkillIndexConflict("version must be dotted numbers", "skill_version_invalid")
    return tuple(int(p) for p in parts)


def _version_gt(left: str, right: str) -> bool:
    a, b = _version_tuple(left), _version_tuple(right)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _skill_id(value: Any) -> str:
    sid = str(value or "").strip()
    if not _SKILL_ID_RE.fullmatch(sid) or ".." in sid:
        raise ValueError("skill_id must be a safe slug")
    return sid


def _files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("files must be a non-empty list")
    out = []
    for item in value:
        path = str((item or {}).get("path") or "")
        sha = str((item or {}).get("sha256") or "")
        if not path or path.startswith("/") or ".." in path.split("/") or not _SHA_RE.fullmatch(sha):
            raise ValueError(f"invalid file entry: {path!r}")
        out.append({"path": path, "sha256": sha, "size": int(item.get("size") or 0)})
    if "SKILL.md" not in {f["path"] for f in out}:
        raise ValueError("files must include SKILL.md")
    return sorted(out, key=lambda f: f["path"])


def _prefix(value: Any) -> str:
    prefix = str(value or "")
    if not _PREFIX_RE.fullmatch(prefix) or ".." in prefix.split("/"):
        raise ValueError("object_prefix must look like skills/.../")
    return prefix


def _sha(value: Any) -> str:
    sha = str(value or "")
    if not _SHA_RE.fullmatch(sha):
        raise ValueError("content_sha must be a sha256 hex digest")
    return sha


def _row(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    for key in ("id", "draft_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at", "decided_at"):
        if out.get(key) is not None:
            out[key] = out[key].strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def _check_catalog_rules(cur, *, skill_id: str, version: str, category: str, revision_of: str | None) -> None:
    cur.execute(
        f"SELECT {_COLUMNS} FROM skill_versions WHERE skill_id = %s AND status = 'active' FOR UPDATE",
        (skill_id,),
    )
    live = cur.fetchone()
    if revision_of:
        if revision_of != skill_id:
            raise SkillIndexConflict(f"revision_of must equal skill id: {skill_id}", "skill_revision_mismatch")
        if live is None:
            raise SkillIndexConflict(f"revision_of is not a live skill: {revision_of}", "skill_revision_not_live")
        if not _version_gt(version, live["version"]):
            raise SkillIndexConflict("revision draft must bump version before proposing", "skill_version_not_bumped")
        if category != live["category"]:
            raise SkillIndexConflict("revision draft category must match the live skill category", "skill_category_changed")
    elif live is not None:
        raise SkillIndexConflict(f"skill id already exists: {skill_id}", "skill_learn_use_revision")


def list_skill_index(conn, params: dict[str, Any]) -> dict[str, Any]:
    statuses = [s for s in str(params.get("status") or "active,draft").split(",") if s]
    if any(s not in _STATUSES for s in statuses):
        raise ValueError(f"status must be among {', '.join(_STATUSES)}")
    clauses, values = ["status = ANY(%s)"], [statuses]
    if params.get("skill_id"):
        clauses.append("skill_id = %s")
        values.append(_skill_id(params["skill_id"]))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT generation FROM shared_file_namespaces WHERE namespace = %s", (SKILLS_NAMESPACE,)
        )
        gen = cur.fetchone()
        cur.execute(
            f"SELECT {_COLUMNS} FROM skill_versions WHERE {' AND '.join(clauses)} ORDER BY skill_id, created_at",
            values,
        )
        rows = [_row(r) for r in cur.fetchall()]
    conn.commit()
    return {"generation": int(gen["generation"]) if gen else 0, "skills": rows}


def upsert_draft(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Create or replace the open draft for a skill; returns the old draft prefix to clean up."""
    skill_id = _skill_id(body.get("skill_id"))
    version = str(body.get("version") or "").strip()
    category = str(body.get("category") or "").strip()
    leaf = str(body.get("leaf") or skill_id.split("/")[-1]).strip()
    layout = str(body.get("layout") or "folder")
    tenant = str(body.get("tenant_id") or "default").strip()
    revision_of = str(body.get("revision_of") or "").strip() or None
    if not category or not leaf or layout not in {"file", "folder"}:
        raise ValueError("category, leaf and a file or folder layout are required")
    _version_tuple(version)
    files, prefix, sha = _files(body.get("files")), _prefix(body.get("object_prefix")), _sha(body.get("content_sha"))
    with conn.cursor() as cur:
        _check_catalog_rules(cur, skill_id=skill_id, version=version, category=category, revision_of=revision_of)
        cur.execute(
            "SELECT id, tenant_id, object_prefix FROM skill_versions WHERE skill_id = %s AND status = 'draft' FOR UPDATE",
            (skill_id,),
        )
        prior = cur.fetchone()
        if prior is not None and prior["tenant_id"] != tenant:
            conn.rollback()
            raise SkillIndexConflict(f"draft already exists for skill id: {skill_id}", "skill_draft_exists")
        values = (version, category, leaf, layout, prefix, Jsonb(files), sha,
                  str(body.get("source") or ""), revision_of, str(body.get("created_by") or ""))
        if prior is None:
            cur.execute(
                f"""
                INSERT INTO skill_versions (tenant_id, skill_id, version, category, leaf, layout, status, object_prefix,
                                            files, content_sha, source, revision_of, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (tenant, skill_id, *values),
            )
        else:
            cur.execute(
                f"""
                UPDATE skill_versions
                   SET version = %s, category = %s, leaf = %s, layout = %s, object_prefix = %s, files = %s,
                       content_sha = %s, source = %s, revision_of = %s, created_by = %s, updated_at = now()
                 WHERE id = %s
                RETURNING {_COLUMNS}
                """,
                (*values, prior["id"]),
            )
        row = cur.fetchone()
        generation = bump_generation(cur, SKILLS_NAMESPACE)
    conn.commit()
    previous = prior["object_prefix"] if prior is not None and prior["object_prefix"] != prefix else None
    return {"skill": _row(row), "previous_prefix": previous, "generation": generation}


def _open_draft(cur, draft_id: Any) -> dict[str, Any]:
    try:
        did = str(uuid.UUID(str(draft_id)))
    except ValueError as exc:
        raise LookupError("draft not found") from exc
    cur.execute(f"SELECT {_COLUMNS} FROM skill_versions WHERE id = %s FOR UPDATE", (did,))
    row = cur.fetchone()
    if row is None:
        raise LookupError("draft not found")
    if row["status"] != "draft":
        raise SkillIndexConflict(f"draft is already {row['status']}", "skill_draft_decided")
    return row


def reject_draft(conn, draft_id: Any, body: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        try:
            row = _open_draft(cur, draft_id)
        except Exception:
            conn.rollback()
            raise
        cur.execute(
            f"""
            UPDATE skill_versions
               SET status = 'rejected', decided_by = %s, decided_at = now(), reason = %s, updated_at = now()
             WHERE id = %s
            RETURNING {_COLUMNS}
            """,
            (str(body.get("reviewer") or ""), str(body.get("reason") or "")[:2000], row["id"]),
        )
        updated = cur.fetchone()
        generation = bump_generation(cur, SKILLS_NAMESPACE)
    conn.commit()
    return {"skill": _row(updated), "generation": generation}


def approve_draft(conn, draft_id: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Point a new live row at the immutable version prefix and supersede the old live row, atomically."""
    files, prefix, sha = _files(body.get("files")), _prefix(body.get("object_prefix")), _sha(body.get("content_sha"))
    reviewer = str(body.get("reviewer") or "").strip()
    if not reviewer:
        raise ValueError("reviewer is required")
    with conn.cursor() as cur:
        try:
            draft = _open_draft(cur, draft_id)
            if prefix.startswith("skills/drafts/"):
                raise ValueError("approved content must live under an immutable version prefix")
            _check_catalog_rules(
                cur,
                skill_id=draft["skill_id"],
                version=draft["version"],
                category=draft["category"],
                revision_of=draft["revision_of"],
            )
        except Exception:
            conn.rollback()
            raise
        cur.execute(
            """
            UPDATE skill_versions SET status = 'superseded', updated_at = now()
             WHERE skill_id = %s AND status = 'active'
            RETURNING id
            """,
            (draft["skill_id"],),
        )
        superseded = cur.fetchone()
        cur.execute(
            f"""
            UPDATE skill_versions SET status = 'approved', decided_by = %s, decided_at = now(), updated_at = now()
             WHERE id = %s
            """,
            (reviewer, draft["id"]),
        )
        cur.execute(
            f"""
            INSERT INTO skill_versions (tenant_id, skill_id, version, category, leaf, layout, status, object_prefix,
                                        files, content_sha, source, revision_of, draft_id, created_by,
                                        decided_by, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s, now())
            RETURNING {_COLUMNS}
            """,
            (draft["tenant_id"], draft["skill_id"], draft["version"], draft["category"], draft["leaf"],
             str(body.get("layout") or draft["layout"]), prefix, Jsonb(files), sha, draft["source"],
             draft["revision_of"], draft["id"], draft["created_by"], reviewer),
        )
        live = cur.fetchone()
        generation = bump_generation(cur, SKILLS_NAMESPACE)
    conn.commit()
    return {
        "skill": _row(live),
        "draft_prefix": draft["object_prefix"],
        "superseded_id": str(superseded["id"]) if superseded else None,
        "generation": generation,
    }
