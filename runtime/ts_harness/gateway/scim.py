"""SCIM 2.0 (RFC 7644) subset — Users and Groups provisioning.

Storage strategy
----------------
* The ``ScimStore`` class is an in-memory store suitable for unit tests and
  single-process deployments.
* The ``ScimUser`` / ``ScimGroup`` dataclasses mirror the ``scim_users`` /
  ``scim_groups`` Postgres tables defined in ``database/migrations/009_scim.sql``
  so swapping in a DB-backed store is a clean interface replacement.

Supported operations
--------------------
Users:  list, create, get, update (replace)
Groups: list, create

The SCIM resource schema follows RFC 7643 §4 (Users) and §5 (Groups).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Resource models
# ---------------------------------------------------------------------------

@dataclass
class ScimUser:
    id: str
    tenant_id: str
    external_id: str
    user_name: str
    display_name: str | None = None
    email: str | None = None
    active: bool = True
    roles: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_scim(self) -> dict[str, Any]:
        """Serialise to SCIM 2.0 User resource representation."""
        doc: dict[str, Any] = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "id": self.id,
            "externalId": self.external_id,
            "userName": self.user_name,
            "active": self.active,
            "meta": {
                "resourceType": "User",
                "created": self.created_at,
                "lastModified": self.updated_at,
            },
        }
        if self.display_name:
            doc["displayName"] = self.display_name
        if self.email:
            doc["emails"] = [{"value": self.email, "primary": True}]
        if self.roles:
            doc["roles"] = [{"value": r} for r in self.roles]
        return doc

    @classmethod
    def from_scim(cls, data: dict[str, Any], tenant_id: str) -> "ScimUser":
        """Construct a ``ScimUser`` from a SCIM User resource payload."""
        now = _now()
        emails = data.get("emails") or []
        email = emails[0].get("value") if emails else data.get("email")
        roles_raw = data.get("roles") or []
        roles = [r["value"] if isinstance(r, dict) else str(r) for r in roles_raw]
        return cls(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            external_id=str(data.get("externalId") or data.get("id") or uuid.uuid4()),
            user_name=str(data["userName"]),
            display_name=data.get("displayName"),
            email=email,
            active=bool(data.get("active", True)),
            roles=roles,
            metadata={k: v for k, v in data.items() if k not in _USER_CORE_FIELDS},
            created_at=now,
            updated_at=now,
        )


_USER_CORE_FIELDS = frozenset(
    {"schemas", "id", "externalId", "userName", "displayName", "emails",
     "email", "active", "roles", "meta"}
)


@dataclass
class ScimGroup:
    id: str
    tenant_id: str
    external_id: str
    display_name: str
    member_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_scim(self) -> dict[str, Any]:
        """Serialise to SCIM 2.0 Group resource representation."""
        return {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "id": self.id,
            "externalId": self.external_id,
            "displayName": self.display_name,
            "members": [{"value": mid} for mid in self.member_ids],
            "meta": {
                "resourceType": "Group",
                "created": self.created_at,
                "lastModified": self.updated_at,
            },
        }

    @classmethod
    def from_scim(cls, data: dict[str, Any], tenant_id: str) -> "ScimGroup":
        now = _now()
        members = data.get("members") or []
        member_ids = [
            m["value"] if isinstance(m, dict) else str(m) for m in members
        ]
        return cls(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            external_id=str(data.get("externalId") or data.get("id") or uuid.uuid4()),
            display_name=str(data["displayName"]),
            member_ids=member_ids,
            metadata={},
            created_at=now,
            updated_at=now,
        )


# ---------------------------------------------------------------------------
# SCIM exceptions
# ---------------------------------------------------------------------------

class ScimError(Exception):
    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.status = status
        self.detail = detail

    def to_scim(self) -> dict[str, Any]:
        return {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "detail": self.detail,
            "status": str(self.status),
        }


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

class ScimStore:
    """Thread-unsafe in-memory SCIM store for tests and single-process use.

    Replace with a Postgres-backed implementation that mirrors
    the ``scim_users`` / ``scim_groups`` schema from 009_scim.sql for
    multi-process or persistent deployments.
    """

    def __init__(self) -> None:
        self._users: dict[str, ScimUser] = {}
        self._groups: dict[str, ScimGroup] = {}

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def list_users(self, tenant_id: str, *, start_index: int = 1, count: int = 100) -> dict[str, Any]:
        users = [u for u in self._users.values() if u.tenant_id == tenant_id]
        page = users[start_index - 1 : start_index - 1 + count]
        return _list_response("User", [u.to_scim() for u in page], total=len(users))

    def create_user(self, tenant_id: str, data: dict[str, Any]) -> ScimUser:
        if "userName" not in data:
            raise ScimError("userName is required", status=400)
        if self._find_user_by_username(tenant_id, data["userName"]) is not None:
            raise ScimError(
                f"User with userName={data['userName']!r} already exists", status=409
            )
        user = ScimUser.from_scim(data, tenant_id)
        self._users[user.id] = user
        return user

    def get_user(self, tenant_id: str, user_id: str) -> ScimUser:
        user = self._users.get(user_id)
        if user is None or user.tenant_id != tenant_id:
            raise ScimError(f"User {user_id!r} not found", status=404)
        return user

    def update_user(self, tenant_id: str, user_id: str, data: dict[str, Any]) -> ScimUser:
        user = self.get_user(tenant_id, user_id)
        now = _now()
        emails = data.get("emails") or []
        email = emails[0].get("value") if emails else data.get("email", user.email)
        roles_raw = data.get("roles") or []
        roles = [r["value"] if isinstance(r, dict) else str(r) for r in roles_raw] or user.roles
        updated = ScimUser(
            id=user.id,
            tenant_id=user.tenant_id,
            external_id=str(data.get("externalId") or user.external_id),
            user_name=str(data.get("userName") or user.user_name),
            display_name=data.get("displayName") or user.display_name,
            email=email,
            active=bool(data.get("active", user.active)),
            roles=roles,
            metadata=user.metadata,
            created_at=user.created_at,
            updated_at=now,
        )
        self._users[user.id] = updated
        return updated

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def list_groups(self, tenant_id: str, *, start_index: int = 1, count: int = 100) -> dict[str, Any]:
        groups = [g for g in self._groups.values() if g.tenant_id == tenant_id]
        page = groups[start_index - 1 : start_index - 1 + count]
        return _list_response("Group", [g.to_scim() for g in page], total=len(groups))

    def create_group(self, tenant_id: str, data: dict[str, Any]) -> ScimGroup:
        if "displayName" not in data:
            raise ScimError("displayName is required", status=400)
        group = ScimGroup.from_scim(data, tenant_id)
        self._groups[group.id] = group
        return group

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def roles_and_groups(self, *, email: str | None = None, external_id: str | None = None) -> tuple[list[str], list[str]]:
        """Role names on the SCIM user, and display names of groups that include them."""
        wanted_email = str(email or "").strip().lower()
        wanted_external = str(external_id or "").strip()
        user = None
        for candidate in self._users.values():
            if wanted_email and str(candidate.email or "").strip().lower() == wanted_email:
                user = candidate
                break
            if wanted_external and str(candidate.external_id or "").strip() == wanted_external:
                user = candidate
                break
        if user is None:
            return [], []
        groups = [
            group.display_name
            for group in self._groups.values()
            if group.tenant_id == user.tenant_id and user.id in set(group.member_ids)
        ]
        return list(user.roles), groups

    def _find_user_by_username(self, tenant_id: str, user_name: str) -> ScimUser | None:
        for user in self._users.values():
            if user.tenant_id == tenant_id and user.user_name == user_name:
                return user
        return None


# ---------------------------------------------------------------------------
# Module-level default store (swap out in tests / production init)
# ---------------------------------------------------------------------------

_default_store = ScimStore()


def get_scim_store() -> ScimStore:
    return _default_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _list_response(resource_type: str, resources: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": total,
        "startIndex": 1,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }
