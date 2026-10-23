"""SCIM 2.0 Users and Groups on the ``scim_users`` and ``scim_groups`` tables.

Entra ID and Okta push people and groups here. Deactivating or deleting a
user disables them in the people directory at once; group changes reach team
memberships at the next directory sync, which maps group names to teams.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from memory_routes.directory import disable_directory_users

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

_FILTER_RE = re.compile(r'^\s*([A-Za-z.]+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$')
_MEMBER_FILTER_RE = re.compile(r'^members\[value eq "([^"]+)"\]$', re.I)
_USER_FILTERS = {"username": "user_name", "externalid": "external_id", "emails.value": "email", "id": "id"}
_GROUP_FILTERS = {"displayname": "display_name", "externalid": "external_id", "id": "id"}


class ScimError(Exception):
    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status = status

    def to_scim(self) -> dict[str, Any]:
        return {"schemas": [ERROR_SCHEMA], "detail": self.detail, "status": str(self.status)}


def _tenant(value: Any) -> str:
    return str(value or "default").strip() or "default"


def _uuid_or_404(value: Any, kind: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ScimError(f"{kind} {value!r} not found", 404) from exc


def _bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _primary_email(data: dict[str, Any]) -> str | None:
    emails = data.get("emails")
    if isinstance(emails, list) and emails:
        primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), emails[0])
        value = primary.get("value") if isinstance(primary, dict) else primary
        return str(value).strip().lower() or None
    if data.get("email"):
        return str(data["email"]).strip().lower()
    return None


def _roles(data: dict[str, Any]) -> list[str]:
    raw = data.get("roles") or []
    return [str(r.get("value") if isinstance(r, dict) else r) for r in raw if r]


def _ts(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _user_doc(row: dict[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(row["id"]),
        "externalId": row.get("external_id"),
        "userName": row.get("user_name"),
        "active": bool(row.get("active")),
        "meta": {"resourceType": "User", "created": _ts(row.get("created_at")), "lastModified": _ts(row.get("updated_at"))},
    }
    if row.get("display_name"):
        doc["displayName"] = row["display_name"]
    if row.get("email"):
        doc["emails"] = [{"value": row["email"], "primary": True}]
    if row.get("roles"):
        doc["roles"] = [{"value": r} for r in row["roles"]]
    return doc


def _group_doc(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemas": [GROUP_SCHEMA],
        "id": str(row["id"]),
        "externalId": row.get("external_id"),
        "displayName": row.get("display_name"),
        "members": [{"value": str(m)} for m in row.get("member_ids") or []],
        "meta": {"resourceType": "Group", "created": _ts(row.get("created_at")), "lastModified": _ts(row.get("updated_at"))},
    }


def _list(docs: list[dict[str, Any]], total: int, start_index: int) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(docs),
        "Resources": docs,
    }


def _filter_clause(raw: str | None, allowed: dict[str, str]) -> tuple[str, list[Any]]:
    if not raw:
        return "", []
    found = _FILTER_RE.match(raw)
    column = allowed.get(found.group(1).lower()) if found else None
    if not found or not column:
        raise ScimError(f"unsupported filter: {raw}", 400)
    value = found.group(2).replace('\\"', '"')
    if column == "id":
        return " AND id = %s", [_uuid_or_404(value, "resource")]
    if column in {"user_name", "email", "display_name"}:
        return f" AND lower({column}) = lower(%s)", [value]
    return f" AND {column} = %s", [value]


def _page(start_index: Any, count: Any) -> tuple[int, int]:
    try:
        start = max(1, int(start_index or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        size = max(0, min(int(count if count is not None else 100), 500))
    except (TypeError, ValueError):
        size = 100
    return start, size


# ── Users ──────────────────────────────────────────────────────────────────


def list_users(conn, *, tenant_id: str, filter: str | None = None, start_index: Any = 1, count: Any = 100) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    clause, params = _filter_clause(filter, _USER_FILTERS)
    start, size = _page(start_index, count)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM scim_users WHERE tenant_id = %s{clause}", [tenant_id, *params])
        total = int(cur.fetchone()["n"])
        cur.execute(
            f"SELECT * FROM scim_users WHERE tenant_id = %s{clause} ORDER BY created_at, id OFFSET %s LIMIT %s",
            [tenant_id, *params, start - 1, size],
        )
        docs = [_user_doc(r) for r in cur.fetchall()]
    return _list(docs, total, start)


def _get_user_row(cur, tenant_id: str, user_id: str) -> dict[str, Any]:
    cur.execute("SELECT * FROM scim_users WHERE tenant_id = %s AND id = %s", (tenant_id, _uuid_or_404(user_id, "User")))
    row = cur.fetchone()
    if not row:
        raise ScimError(f"User {user_id!r} not found", 404)
    return row


def get_user(conn, *, tenant_id: str, user_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        return _user_doc(_get_user_row(cur, _tenant(tenant_id), user_id))


def _deprovision(conn, tenant_id: str, email: str | None) -> None:
    if email:
        disable_directory_users(conn, {"tenant_id": tenant_id, "emails": [email]})


def create_user(conn, *, tenant_id: str, data: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    user_name = str(data.get("userName") or "").strip()
    if not user_name:
        raise ScimError("userName is required", 400)
    external_id = str(data.get("externalId") or user_name)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM scim_users WHERE tenant_id = %s AND (lower(user_name) = lower(%s) OR external_id = %s)",
            (tenant_id, user_name, external_id),
        )
        if cur.fetchone():
            raise ScimError(f"User with userName={user_name!r} already exists", 409)
        cur.execute(
            """
            INSERT INTO scim_users (tenant_id, external_id, user_name, display_name, email, active, roles, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """,
            (
                tenant_id,
                external_id,
                user_name,
                data.get("displayName"),
                _primary_email(data),
                _bool(data.get("active")),
                _roles(data),
                json.dumps({"name": data.get("name")} if data.get("name") else {}),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    if not row["active"]:
        _deprovision(conn, tenant_id, row.get("email"))
    return _user_doc(row)


def _save_user(conn, tenant_id: str, before: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE scim_users SET external_id = %s, user_name = %s, display_name = %s, email = %s,
                   active = %s, roles = %s, updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (
                fields["external_id"],
                fields["user_name"],
                fields["display_name"],
                fields["email"],
                fields["active"],
                fields["roles"],
                before["id"],
            ),
        )
        row = cur.fetchone()
    conn.commit()
    if before.get("active") and not row["active"]:
        _deprovision(conn, tenant_id, row.get("email") or before.get("email"))
    return _user_doc(row)


def replace_user(conn, *, tenant_id: str, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    with conn.cursor() as cur:
        before = _get_user_row(cur, tenant_id, user_id)
    return _save_user(
        conn,
        tenant_id,
        before,
        {
            "external_id": str(data.get("externalId") or before["external_id"]),
            "user_name": str(data.get("userName") or before["user_name"]),
            "display_name": data.get("displayName"),
            "email": _primary_email(data),
            "active": _bool(data.get("active")),
            "roles": _roles(data),
        },
    )


def patch_user(conn, *, tenant_id: str, user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    with conn.cursor() as cur:
        before = _get_user_row(cur, tenant_id, user_id)
    fields = {
        "external_id": before["external_id"],
        "user_name": before["user_name"],
        "display_name": before.get("display_name"),
        "email": before.get("email"),
        "active": bool(before.get("active")),
        "roles": list(before.get("roles") or []),
    }
    for op in data.get("Operations") or []:
        kind = str(op.get("op") or "").lower()
        path = str(op.get("path") or "")
        value = op.get("value")
        if kind not in {"add", "replace", "remove"}:
            raise ScimError(f"unsupported op: {op.get('op')}", 400)
        updates = value if not path and isinstance(value, dict) else {path: value}
        for key, item in updates.items():
            lowered = key.lower()
            if lowered == "active":
                fields["active"] = False if kind == "remove" else _bool(item)
            elif lowered == "username":
                fields["user_name"] = str(item or fields["user_name"])
            elif lowered == "displayname":
                fields["display_name"] = None if kind == "remove" else item
            elif lowered == "externalid":
                fields["external_id"] = str(item or fields["external_id"])
            elif lowered.startswith("emails"):
                if kind == "remove":
                    fields["email"] = None
                elif isinstance(item, list):
                    fields["email"] = _primary_email({"emails": item})
                elif item:
                    fields["email"] = str(item).strip().lower()
            elif lowered == "roles":
                fields["roles"] = [] if kind == "remove" else _roles({"roles": item if isinstance(item, list) else [item]})
    return _save_user(conn, tenant_id, before, fields)


def delete_user(conn, *, tenant_id: str, user_id: str) -> None:
    tenant_id = _tenant(tenant_id)
    with conn.cursor() as cur:
        before = _get_user_row(cur, tenant_id, user_id)
        cur.execute(
            "UPDATE scim_groups SET member_ids = array_remove(member_ids, %s::uuid), updated_at = now() WHERE tenant_id = %s",
            (str(before["id"]), tenant_id),
        )
        cur.execute("DELETE FROM scim_users WHERE id = %s", (before["id"],))
    conn.commit()
    _deprovision(conn, tenant_id, before.get("email"))


# ── Groups ─────────────────────────────────────────────────────────────────


def list_groups(conn, *, tenant_id: str, filter: str | None = None, start_index: Any = 1, count: Any = 100) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    clause, params = _filter_clause(filter, _GROUP_FILTERS)
    start, size = _page(start_index, count)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM scim_groups WHERE tenant_id = %s{clause}", [tenant_id, *params])
        total = int(cur.fetchone()["n"])
        cur.execute(
            f"SELECT * FROM scim_groups WHERE tenant_id = %s{clause} ORDER BY created_at, id OFFSET %s LIMIT %s",
            [tenant_id, *params, start - 1, size],
        )
        docs = [_group_doc(r) for r in cur.fetchall()]
    return _list(docs, total, start)


def _get_group_row(cur, tenant_id: str, group_id: str) -> dict[str, Any]:
    cur.execute("SELECT * FROM scim_groups WHERE tenant_id = %s AND id = %s", (tenant_id, _uuid_or_404(group_id, "Group")))
    row = cur.fetchone()
    if not row:
        raise ScimError(f"Group {group_id!r} not found", 404)
    return row


def get_group(conn, *, tenant_id: str, group_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        return _group_doc(_get_group_row(cur, _tenant(tenant_id), group_id))


def _member_ids(cur, tenant_id: str, raw: Any) -> list[str]:
    wanted: list[str] = []
    for item in raw if isinstance(raw, list) else [raw]:
        value = item.get("value") if isinstance(item, dict) else item
        if value:
            wanted.append(_uuid_or_404(value, "User"))
    if not wanted:
        return []
    cur.execute("SELECT id FROM scim_users WHERE tenant_id = %s AND id = ANY(%s::uuid[])", (tenant_id, wanted))
    known = {str(r["id"]) for r in cur.fetchall()}
    return [w for w in dict.fromkeys(wanted) if w in known]


def create_group(conn, *, tenant_id: str, data: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    name = str(data.get("displayName") or "").strip()
    if not name:
        raise ScimError("displayName is required", 400)
    external_id = str(data.get("externalId") or name)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM scim_groups WHERE tenant_id = %s AND (lower(display_name) = lower(%s) OR external_id = %s)",
            (tenant_id, name, external_id),
        )
        if cur.fetchone():
            raise ScimError(f"Group {name!r} already exists", 409)
        members = _member_ids(cur, tenant_id, data.get("members") or [])
        cur.execute(
            """
            INSERT INTO scim_groups (tenant_id, external_id, display_name, member_ids)
            VALUES (%s, %s, %s, %s::uuid[]) RETURNING *
            """,
            (tenant_id, external_id, name, members),
        )
        row = cur.fetchone()
    conn.commit()
    return _group_doc(row)


def _save_group(conn, group_id: Any, name: str, members: list[str]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scim_groups SET display_name = %s, member_ids = %s::uuid[], updated_at = now() WHERE id = %s RETURNING *",
            (name, members, group_id),
        )
        row = cur.fetchone()
    conn.commit()
    return _group_doc(row)


def replace_group(conn, *, tenant_id: str, group_id: str, data: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    with conn.cursor() as cur:
        before = _get_group_row(cur, tenant_id, group_id)
        members = _member_ids(cur, tenant_id, data.get("members") or [])
    return _save_group(conn, before["id"], str(data.get("displayName") or before["display_name"]), members)


def patch_group(conn, *, tenant_id: str, group_id: str, data: dict[str, Any]) -> dict[str, Any]:
    tenant_id = _tenant(tenant_id)
    with conn.cursor() as cur:
        before = _get_group_row(cur, tenant_id, group_id)
        name = str(before["display_name"])
        members = [str(m) for m in before.get("member_ids") or []]
        for op in data.get("Operations") or []:
            kind = str(op.get("op") or "").lower()
            path = str(op.get("path") or "")
            value = op.get("value")
            member_filter = _MEMBER_FILTER_RE.match(path)
            if kind not in {"add", "replace", "remove"}:
                raise ScimError(f"unsupported op: {op.get('op')}", 400)
            if member_filter and kind == "remove":
                members = [m for m in members if m != member_filter.group(1)]
            elif path.lower() == "members":
                ids = _member_ids(cur, tenant_id, value or [])
                if kind == "add":
                    members = list(dict.fromkeys([*members, *ids]))
                elif kind == "replace":
                    members = ids
                elif value:
                    members = [m for m in members if m not in set(ids)]
                else:
                    members = []
            elif path.lower() == "displayname" and kind != "remove":
                name = str(value or name)
            elif not path and isinstance(value, dict):
                if value.get("displayName"):
                    name = str(value["displayName"])
                if "members" in value:
                    members = _member_ids(cur, tenant_id, value.get("members") or [])
    return _save_group(conn, before["id"], name, members)


def delete_group(conn, *, tenant_id: str, group_id: str) -> None:
    tenant_id = _tenant(tenant_id)
    with conn.cursor() as cur:
        before = _get_group_row(cur, tenant_id, group_id)
        cur.execute("DELETE FROM scim_groups WHERE id = %s", (before["id"],))
    conn.commit()


def snapshot(conn, *, tenant_id: str) -> dict[str, Any]:
    """Every provisioned person with their group names, for directory sync."""
    tenant_id = _tenant(tenant_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.user_name, u.display_name, u.email, u.active, u.roles,
                   COALESCE(array_agg(g.display_name ORDER BY g.display_name)
                            FILTER (WHERE g.id IS NOT NULL), '{}') AS groups
            FROM scim_users u
            LEFT JOIN scim_groups g ON g.tenant_id = u.tenant_id AND u.id = ANY(g.member_ids)
            WHERE u.tenant_id = %s
            GROUP BY u.id
            ORDER BY u.user_name
            """,
            (tenant_id,),
        )
        users = [
            {
                "email": (r.get("email") or (r["user_name"] if "@" in str(r["user_name"]) else None)),
                "display_name": r.get("display_name"),
                "roles": list(r.get("roles") or []),
                "groups": list(r.get("groups") or []),
                "disabled": not r.get("active"),
            }
            for r in cur.fetchall()
        ]
    return {"users": users}
