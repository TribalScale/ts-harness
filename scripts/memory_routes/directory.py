"""People directory and teams for sharing and Slack linking."""

from __future__ import annotations

import json
import re
from typing import Any

TEAM_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TEAM_ROLES = {"member", "approver", "manager"}


def _tenant(value: Any) -> str:
    return str(value or "default").strip() or "default"


def _limit(value: Any, default: int = 20, ceiling: int = 100) -> int:
    try:
        return max(1, min(int(value), ceiling))
    except (TypeError, ValueError):
        return default


def _clean_teams(raw: Any) -> list[tuple[str, str]]:
    teams: list[tuple[str, str]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip().lower()
        role = str(item.get("role") or "member").strip().lower()
        if TEAM_SLUG_RE.fullmatch(slug) and role in TEAM_ROLES and (slug, role) not in teams:
            teams.append((slug, role))
    return teams


def team_display_name(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part) or slug


def ensure_team(cur, tenant_id: str, slug: str, *, source: str = "groups", display_name: str | None = None) -> str:
    cur.execute(
        """
        INSERT INTO teams (tenant_id, slug, display_name, source)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tenant_id, slug) DO UPDATE SET slug = EXCLUDED.slug
        RETURNING id
        """,
        (tenant_id, slug, display_name or team_display_name(slug), source),
    )
    return str(cur.fetchone()["id"])


def replace_memberships(
    cur, tenant_id: str, subject: str, teams: list[tuple[str, str]], *, source: str = "groups"
) -> None:
    """Make *teams* the subject's full membership from *source*; other sources are kept."""
    cur.execute(
        "DELETE FROM team_members WHERE tenant_id = %s AND subject = %s AND source = %s",
        (tenant_id, subject, source),
    )
    for slug, role in teams:
        team_id = ensure_team(cur, tenant_id, slug, source=source)
        cur.execute(
            """
            INSERT INTO team_members (team_id, tenant_id, subject, role, source, synced_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (team_id, subject, role) DO UPDATE
              SET source = EXCLUDED.source, synced_at = now()
            """,
            (team_id, tenant_id, subject, role, source),
        )


def upsert_directory_user(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Record a person seen at sign-in (or from a directory sync) and their teams."""
    tenant_id = _tenant(body.get("tenant_id"))
    subject = str(body.get("subject") or "").strip()
    if not subject:
        raise ValueError("subject is required")
    email = str(body.get("email") or "").strip().lower() or None
    display_name = str(body.get("display_name") or "").strip() or None
    github_login = str(body.get("github_login") or "").strip() or None
    source = str(body.get("source") or "seen_users").strip() or "seen_users"
    teams = _clean_teams(body.get("teams"))
    roles = body.get("roles")
    roles_json = (
        json.dumps(sorted({str(r).strip().lower() for r in roles if str(r).strip()}))
        if isinstance(roles, list)
        else None
    )
    seen = bool(body.get("seen", True))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO directory_users (tenant_id, subject, email, display_name, roles, source, last_seen_at, github_login)
            VALUES (%s, %s, %s, %s, COALESCE(%s::jsonb, '[]'::jsonb), %s, CASE WHEN %s THEN now() END, %s)
            ON CONFLICT (tenant_id, subject) DO UPDATE SET
              email = COALESCE(EXCLUDED.email, directory_users.email),
              display_name = COALESCE(EXCLUDED.display_name, directory_users.display_name),
              github_login = COALESCE(EXCLUDED.github_login, directory_users.github_login),
              roles = CASE WHEN %s::jsonb IS NULL THEN directory_users.roles ELSE EXCLUDED.roles END,
              last_seen_at = COALESCE(EXCLUDED.last_seen_at, directory_users.last_seen_at),
              updated_at = now()
            RETURNING disabled_at
            """,
            (tenant_id, subject, email, display_name, roles_json, source, seen, github_login, roles_json),
        )
        row = cur.fetchone()
        if body.get("teams") is not None:
            replace_memberships(cur, tenant_id, subject, teams, source=str(body.get("teams_source") or "groups"))
    conn.commit()
    return {
        "ok": True,
        "subject": subject,
        "teams": [{"slug": slug, "role": role} for slug, role in teams],
        "disabled": bool(row and row.get("disabled_at")),
    }


def _user_teams(cur, tenant_id: str, subject: str) -> list[dict[str, str]]:
    cur.execute(
        """
        SELECT t.slug, m.role
        FROM team_members m JOIN teams t ON t.id = m.team_id
        WHERE m.tenant_id = %s AND m.subject = %s
        ORDER BY t.slug, m.role
        """,
        (tenant_id, subject),
    )
    return [{"slug": str(r["slug"]), "role": str(r["role"])} for r in cur.fetchall()]


def _user_payload(cur, row: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(row["tenant_id"])
    return {
        "subject": str(row["subject"]),
        "email": row.get("email"),
        "display_name": row.get("display_name"),
        "github_login": row.get("github_login"),
        "roles": list(row.get("roles") or []),
        "disabled": bool(row.get("disabled_at")),
        "last_seen_at": row.get("last_seen_at"),
        "teams": _user_teams(cur, tenant_id, str(row["subject"])),
    }


def get_directory_user(conn, *, tenant_id: str, subject: str = "", email: str = "") -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    subject = str(subject or "").strip()
    email = str(email or "").strip().lower()
    if not subject and not email:
        raise ValueError("subject or email is required")
    with conn.cursor() as cur:
        if subject:
            cur.execute(
                "SELECT * FROM directory_users WHERE tenant_id = %s AND subject = %s",
                (tenant_id, subject),
            )
        else:
            cur.execute(
                """
                SELECT * FROM directory_users
                WHERE tenant_id = %s AND lower(email) = %s
                ORDER BY disabled_at NULLS FIRST, last_seen_at DESC NULLS LAST
                LIMIT 1
                """,
                (tenant_id, email),
            )
        row = cur.fetchone()
        if not row:
            return {"found": False}
        return {"found": True, "user": _user_payload(cur, row)}


def search_directory(conn, *, tenant_id: str, q: str = "", limit: Any = 20) -> dict[str, Any]:
    """Active people and teams whose name, email or slug contains *q*."""
    tenant_id = _tenant(tenant_id)
    size = _limit(limit)
    pattern = "%" + str(q or "").strip().lower().replace("%", r"\%").replace("_", r"\_") + "%"
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT subject, email, display_name
            FROM directory_users
            WHERE tenant_id = %s AND disabled_at IS NULL
              AND (lower(coalesce(email, '')) LIKE %s
                   OR lower(coalesce(display_name, '')) LIKE %s
                   OR lower(subject) LIKE %s)
            ORDER BY lower(coalesce(display_name, email, subject))
            LIMIT %s
            """,
            (tenant_id, pattern, pattern, pattern, size),
        )
        users = [
            {"subject": str(r["subject"]), "email": r.get("email"), "display_name": r.get("display_name")}
            for r in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT t.slug, t.display_name, count(DISTINCT m.subject) AS member_count
            FROM teams t LEFT JOIN team_members m ON m.team_id = t.id
            WHERE t.tenant_id = %s
              AND (lower(t.slug) LIKE %s OR lower(t.display_name) LIKE %s)
            GROUP BY t.id
            ORDER BY t.slug
            LIMIT %s
            """,
            (tenant_id, pattern, pattern, size),
        )
        teams = [
            {"slug": str(r["slug"]), "display_name": r["display_name"], "member_count": int(r["member_count"])}
            for r in cur.fetchall()
        ]
    return {"users": users, "teams": teams}


SNAPSHOT_MATCHES = ("subject", "email")
DEFAULT_MAX_DISABLE_FRACTION = 0.5


def _disable(cur, tenant_id: str, subject: str) -> None:
    cur.execute(
        "UPDATE directory_users SET disabled_at = COALESCE(disabled_at, now()), updated_at = now() "
        "WHERE tenant_id = %s AND subject = %s",
        (tenant_id, subject),
    )
    cur.execute("DELETE FROM team_members WHERE tenant_id = %s AND subject = %s", (tenant_id, subject))


def disable_directory_users(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Disable people by subject or email at once (SCIM deprovisioning, manual offboarding)."""
    tenant_id = _tenant(body.get("tenant_id"))
    subjects = {str(s).strip() for s in body.get("subjects") or [] if str(s).strip()}
    emails = [str(e).strip().lower() for e in body.get("emails") or [] if str(e).strip()]
    with conn.cursor() as cur:
        if emails:
            cur.execute(
                "SELECT subject FROM directory_users WHERE tenant_id = %s AND lower(email) = ANY(%s)",
                (tenant_id, emails),
            )
            subjects.update(str(r["subject"]) for r in cur.fetchall())
        for subject in sorted(subjects):
            _disable(cur, tenant_id, subject)
    conn.commit()
    return {"ok": True, "disabled": sorted(subjects)}


def apply_directory_snapshot(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Bring directory users in line with an identity source.

    ``users`` items carry ``subject`` (when the source knows the sign-in
    subject), ``email``, ``display_name``, optional ``roles`` and ``teams``,
    and ``disabled``. With ``match: email`` only people who have signed in
    are updated. With ``complete: true`` anyone the source no longer lists
    is disabled, unless that would disable more than ``max_disable_fraction``
    of active people, which usually means the source returned a partial list.
    """
    tenant_id = _tenant(body.get("tenant_id"))
    source = str(body.get("source") or "").strip() or "directory"
    match = str(body.get("match") or "subject").strip().lower()
    if match not in SNAPSHOT_MATCHES:
        raise ValueError("match must be subject or email")
    complete = bool(body.get("complete"))
    try:
        max_fraction = float(body.get("max_disable_fraction", DEFAULT_MAX_DISABLE_FRACTION))
    except (TypeError, ValueError):
        max_fraction = DEFAULT_MAX_DISABLE_FRACTION
    entries = [u for u in body.get("users") or [] if isinstance(u, dict)]
    result: dict[str, Any] = {
        "ok": True,
        "source": source,
        "created": 0,
        "updated": 0,
        "unknown": 0,
        "disabled": [],
        "reenabled": [],
        "disable_skipped": False,
    }
    with conn.cursor() as cur:
        cur.execute(
            "SELECT subject, lower(email) AS email, disabled_at FROM directory_users WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = list(cur.fetchall())
        by_subject = {str(r["subject"]): r for r in rows}
        by_email: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("email"):
                by_email.setdefault(str(row["email"]), []).append(row)
        seen: set[str] = set()
        to_disable: set[str] = set()
        for entry in entries:
            subject = str(entry.get("subject") or "").strip()
            email = str(entry.get("email") or "").strip().lower()
            if match == "subject" and subject:
                targets = [subject]
            else:
                targets = [str(r["subject"]) for r in by_email.get(email, [])] if email else []
            if not targets:
                if match == "subject" and subject:
                    targets = [subject]
                else:
                    result["unknown"] += 1
                    continue
            for target in targets:
                seen.add(target)
                existing = by_subject.get(target)
                if entry.get("disabled"):
                    if existing is not None:
                        to_disable.add(target)
                    continue
                roles = entry.get("roles")
                roles_json = (
                    json.dumps(sorted({str(r).strip().lower() for r in roles if str(r).strip()}))
                    if isinstance(roles, list)
                    else None
                )
                cur.execute(
                    """
                    INSERT INTO directory_users (tenant_id, subject, email, display_name, roles, source, github_login)
                    VALUES (%s, %s, %s, %s, COALESCE(%s::jsonb, '[]'::jsonb), %s, %s)
                    ON CONFLICT (tenant_id, subject) DO UPDATE SET
                      email = COALESCE(EXCLUDED.email, directory_users.email),
                      display_name = COALESCE(EXCLUDED.display_name, directory_users.display_name),
                      github_login = COALESCE(EXCLUDED.github_login, directory_users.github_login),
                      roles = CASE WHEN %s::jsonb IS NULL THEN directory_users.roles ELSE EXCLUDED.roles END,
                      disabled_at = NULL,
                      updated_at = now()
                    """,
                    (
                        tenant_id,
                        target,
                        email or None,
                        str(entry.get("display_name") or "").strip() or None,
                        roles_json,
                        source,
                        str(entry.get("github_login") or "").strip() or None,
                        roles_json,
                    ),
                )
                if existing is None:
                    result["created"] += 1
                else:
                    result["updated"] += 1
                    if existing.get("disabled_at"):
                        result["reenabled"].append(target)
                if entry.get("teams") is not None:
                    replace_memberships(cur, tenant_id, target, _clean_teams(entry.get("teams")))
        if complete:
            active = [s for s, r in by_subject.items() if not r.get("disabled_at")]
            missing = {s for s in active if s not in seen}
            if missing and entries and len(missing) <= max_fraction * len(active):
                to_disable |= missing
            elif missing:
                result["disable_skipped"] = True
                result["would_disable"] = len(missing)
        for subject in sorted(to_disable):
            _disable(cur, tenant_id, subject)
        result["disabled"] = sorted(to_disable)
    conn.commit()
    return result
