"""Program visibility, sharing grants and the access level of an actor.

Access is decided on the program's root (``root_program_id``, or the program
itself), so sharing a program shares its children. An actor is
``{subject, teams: [{slug, role}], is_admin}`` as resolved by the runtime.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

LEVELS = ("none", "read", "write", "approve", "admin")
GRANT_LEVELS = ("read", "write", "approve", "admin")
PRINCIPAL_TYPES = ("user", "team")
VISIBILITIES = ("private", "public")


def rank(level: str | None) -> int:
    try:
        return LEVELS.index(str(level or "none"))
    except ValueError:
        return 0


def _uuid(value: Any, field: str = "program_id") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def parse_teams(raw: Any) -> list[dict[str, str]]:
    """Teams as a list of dicts, or ``slug:role,slug:role`` from a query string."""
    items: list[Any]
    if isinstance(raw, str):
        items = [part for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    teams: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            slug, role = str(item.get("slug") or ""), str(item.get("role") or "member")
        else:
            slug, _, role = str(item).partition(":")
        slug, role = slug.strip().lower(), (role.strip().lower() or "member")
        if slug:
            teams.append({"slug": slug, "role": role})
    return teams


def parse_actor(source: dict[str, Any]) -> dict[str, Any] | None:
    """Actor from a request body (``actor_context``) or flat query values; None means unfiltered."""
    ctx = source.get("actor_context") if isinstance(source.get("actor_context"), dict) else source
    subject = str(ctx.get("subject") or ctx.get("actor_subject") or "").strip()
    is_admin = str(ctx.get("is_admin") or ctx.get("actor_admin") or "").lower() in {"1", "true", "yes"}
    if not subject and not is_admin:
        return None
    return {
        "subject": subject,
        "teams": parse_teams(ctx.get("teams") if "teams" in ctx else ctx.get("actor_teams")),
        "is_admin": is_admin,
    }


def _access_root(cur, program_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT p.id, p.tenant_id, COALESCE(p.root_program_id, p.id) AS access_root_id,
               r.owner_id, r.visibility
        FROM delivery_programs p
        JOIN delivery_programs r ON r.id = COALESCE(p.root_program_id, p.id)
        WHERE p.id = %s
        """,
        (program_id,),
    )
    return cur.fetchone()


def _grants(cur, root_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT principal_type, principal_id, level, granted_by, granted_at
        FROM program_access WHERE program_id = %s
        ORDER BY principal_type, principal_id
        """,
        (root_id,),
    )
    return list(cur.fetchall())


def compute_level(
    *, owner_id: str | None, visibility: str, grants: list[dict[str, Any]], actor: dict[str, Any]
) -> tuple[str, str, str]:
    """Return ``(level, base_level, via)``; ``base_level`` ignores the harness-admin role."""
    subject = str(actor.get("subject") or "")
    roles_by_team: dict[str, set[str]] = {}
    for team in actor.get("teams") or []:
        roles_by_team.setdefault(str(team.get("slug")), set()).add(str(team.get("role") or "member"))
    best, via = "none", "none"

    def offer(level: str, source: str) -> None:
        nonlocal best, via
        if rank(level) > rank(best):
            best, via = level, source

    if subject and owner_id and subject == owner_id:
        offer("admin", "owner")
    for grant in grants:
        level = str(grant["level"])
        if grant["principal_type"] == "user" and subject and grant["principal_id"] == subject:
            offer(level, "user")
        elif grant["principal_type"] == "team" and str(grant["principal_id"]) in roles_by_team:
            if "approver" in roles_by_team[str(grant["principal_id"])] and rank(level) >= rank("write"):
                level = LEVELS[max(rank(level), rank("approve"))]
            offer(level, "team")
    if visibility == "public":
        offer("read", "public")
    base = best
    if actor.get("is_admin"):
        offer("admin", "harness_admin")
    return best, base, via


def program_access_level(conn, program_id: str, actor: dict[str, Any] | None) -> dict[str, Any]:
    pid = _uuid(program_id)
    with conn.cursor() as cur:
        root = _access_root(cur, pid)
        if not root:
            return {"found": False, "level": "none"}
        root_id = str(root["access_root_id"])
        grants = _grants(cur, root_id)
    if actor is None:
        level, base, via = "admin", "admin", "service"
    else:
        level, base, via = compute_level(
            owner_id=root.get("owner_id"), visibility=str(root.get("visibility") or "private"), grants=grants, actor=actor
        )
    return {
        "found": True,
        "program_id": pid,
        "tenant_id": root.get("tenant_id"),
        "access_root_id": root_id,
        "owner_id": root.get("owner_id"),
        "visibility": root.get("visibility"),
        "level": level,
        "base_level": base,
        "via": via,
    }


def list_filter_clause(actor: dict[str, Any] | None, *, alias: str = "delivery_programs") -> tuple[str, list[Any]]:
    """SQL that keeps programs the actor can read; empty when unfiltered."""
    if actor is None or actor.get("is_admin"):
        return "", []
    root = f"COALESCE({alias}.root_program_id, {alias}.id)"
    subject = str(actor.get("subject") or "")
    slugs = sorted({str(t.get("slug")) for t in actor.get("teams") or [] if t.get("slug")})
    clause = f"""EXISTS (
        SELECT 1 FROM delivery_programs access_root
        WHERE access_root.id = {root}
          AND (access_root.visibility = 'public'
               OR (access_root.owner_id IS NOT NULL AND access_root.owner_id = %s)
               OR EXISTS (SELECT 1 FROM program_access g
                          WHERE g.program_id = access_root.id
                            AND ((g.principal_type = 'user' AND g.principal_id = %s)
                                 OR (g.principal_type = 'team' AND g.principal_id = ANY(%s)))))
    )"""
    return clause, [subject, subject, slugs]


def _audit(cur, action_type: str, summary: str, details: dict[str, Any], actor: str) -> None:
    cur.execute(
        """
        INSERT INTO session_audit_log
          (id, session_id, agent_id, action_type, summary, details, occurred_at)
        VALUES (uuid_generate_v4(), %s, %s, %s, %s, %s::jsonb, now())
        """,
        (str(details.get("program_id") or "program"), actor, action_type, summary, json.dumps(details, default=str)),
    )


def _root_or_raise(cur, program_id: str) -> dict[str, Any]:
    root = _access_root(cur, _uuid(program_id))
    if not root:
        raise LookupError("program not found")
    return root


def list_program_access(conn, program_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        root = _root_or_raise(cur, program_id)
        root_id = str(root["access_root_id"])
        grants = _grants(cur, root_id)
        user_ids = [g["principal_id"] for g in grants if g["principal_type"] == "user"]
        if root.get("owner_id"):
            user_ids.append(root["owner_id"])
        names: dict[str, dict[str, Any]] = {}
        if user_ids:
            cur.execute(
                """
                SELECT subject, email, display_name FROM directory_users
                WHERE tenant_id = %s AND subject = ANY(%s)
                """,
                (root.get("tenant_id") or "default", user_ids),
            )
            names = {str(r["subject"]): r for r in cur.fetchall()}
    owner = root.get("owner_id")
    return {
        "program_id": str(root["id"]),
        "access_root_id": root_id,
        "inherited": root_id != str(root["id"]),
        "visibility": root.get("visibility"),
        "owner": {
            "subject": owner,
            "email": (names.get(str(owner)) or {}).get("email"),
            "display_name": (names.get(str(owner)) or {}).get("display_name"),
        }
        if owner
        else None,
        "grants": [
            {
                "principal_type": g["principal_type"],
                "principal_id": g["principal_id"],
                "level": g["level"],
                "granted_by": g.get("granted_by"),
                "granted_at": g.get("granted_at"),
                "email": (names.get(str(g["principal_id"])) or {}).get("email") if g["principal_type"] == "user" else None,
                "display_name": (names.get(str(g["principal_id"])) or {}).get("display_name")
                if g["principal_type"] == "user"
                else None,
            }
            for g in grants
        ],
    }


def grant_program_access(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    principal_type = str(body.get("principal_type") or "").strip().lower()
    principal_id = str(body.get("principal_id") or "").strip()
    level = str(body.get("level") or "").strip().lower()
    actor = str(body.get("actor") or "admin")
    if principal_type not in PRINCIPAL_TYPES:
        raise ValueError("principal_type must be user or team")
    if not principal_id:
        raise ValueError("principal_id is required")
    if principal_type == "team":
        principal_id = principal_id.lower()
    if level not in GRANT_LEVELS:
        raise ValueError(f"level must be one of {', '.join(GRANT_LEVELS)}")
    with conn.cursor() as cur:
        root = _root_or_raise(cur, program_id)
        root_id = str(root["access_root_id"])
        if principal_type == "team":
            cur.execute(
                "SELECT 1 FROM teams WHERE tenant_id = %s AND slug = %s",
                (root.get("tenant_id") or "default", principal_id),
            )
            if not cur.fetchone():
                raise ValueError("team not found")
        cur.execute(
            """
            INSERT INTO program_access (program_id, principal_type, principal_id, level, granted_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (program_id, principal_type, principal_id) DO UPDATE
              SET level = EXCLUDED.level, granted_by = EXCLUDED.granted_by, granted_at = now()
            """,
            (root_id, principal_type, principal_id, level, actor),
        )
        _audit(
            cur,
            "program_access_granted",
            f"Granted {level} on program to {principal_type} {principal_id}",
            {"program_id": root_id, "principal_type": principal_type, "principal_id": principal_id, "level": level},
            actor,
        )
    conn.commit()
    return list_program_access(conn, root_id)


def revoke_program_access(conn, program_id: str, principal_type: str, principal_id: str, body: dict[str, Any]) -> dict[str, Any]:
    principal_type = str(principal_type or "").strip().lower()
    if principal_type not in PRINCIPAL_TYPES:
        raise ValueError("principal_type must be user or team")
    if principal_type == "team":
        principal_id = str(principal_id).lower()
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        root = _root_or_raise(cur, program_id)
        root_id = str(root["access_root_id"])
        cur.execute(
            "DELETE FROM program_access WHERE program_id = %s AND principal_type = %s AND principal_id = %s",
            (root_id, principal_type, principal_id),
        )
        removed = cur.rowcount
        if removed:
            _audit(
                cur,
                "program_access_revoked",
                f"Revoked program access from {principal_type} {principal_id}",
                {"program_id": root_id, "principal_type": principal_type, "principal_id": principal_id},
                actor,
            )
    conn.commit()
    return list_program_access(conn, root_id)


def set_program_visibility(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    visibility = str(body.get("visibility") or "").strip().lower()
    if visibility not in VISIBILITIES:
        raise ValueError("visibility must be private or public")
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        root = _root_or_raise(cur, program_id)
        root_id = str(root["access_root_id"])
        if root.get("visibility") != visibility:
            cur.execute("UPDATE delivery_programs SET visibility = %s WHERE id = %s", (visibility, root_id))
            _audit(
                cur,
                "program_visibility_changed",
                f"Program made {visibility}",
                {"program_id": root_id, "from": root.get("visibility"), "to": visibility},
                actor,
            )
    conn.commit()
    return list_program_access(conn, root_id)


def record_program_admin_access(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        root = _root_or_raise(cur, program_id)
        _audit(
            cur,
            "program_admin_access",
            "Harness admin opened a program they have no grant on",
            {
                "program_id": str(root["id"]),
                "access_root_id": str(root["access_root_id"]),
                "route": str(body.get("route") or ""),
                "need": str(body.get("need") or ""),
            },
            actor,
        )
    conn.commit()
    return {"ok": True}


def task_program(conn, task_id: str) -> dict[str, Any]:
    """The program a workflow task belongs to, walking up parent workflows."""
    tid = _uuid(task_id, "task_id")
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT w.id, w.parent_workflow_id, w.program_id, 0 AS depth
                FROM workflow_tasks t JOIN workflows w ON w.id = t.workflow_id
                WHERE t.id = %s
              UNION ALL
                SELECT w.id, w.parent_workflow_id, w.program_id, chain.depth + 1
                FROM workflows w JOIN chain ON w.id = chain.parent_workflow_id
                WHERE chain.program_id IS NULL AND chain.depth < 20
            )
            SELECT program_id FROM chain WHERE program_id IS NOT NULL ORDER BY depth LIMIT 1
            """,
            (tid,),
        )
        row = cur.fetchone()
    return {"task_id": tid, "program_id": str(row["program_id"]) if row else None}
