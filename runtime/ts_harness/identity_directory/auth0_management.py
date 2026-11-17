"""Auth0 Management API source: users, block status, roles and ``ts-harness-*`` groups.

Auth0 roles whose names start with ``ts-harness-`` are treated as groups
(``ts-harness-admins``, ``ts-harness-team-payments-approvers``); other roles
are harness roles. ``app_metadata.groups`` adds groups set by enterprise
connections or an admin.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx

from ts_harness.identity_directory.base import (
    GROUP_PREFIX,
    DirectoryPerson,
    DirectorySnapshot,
    check,
    http_client,
    lower_email,
)

PER_PAGE = 100
# GET /api/v2/users stops at 1,000 results; beyond that only a user export sees everyone.
SEARCH_CEILING = 1000


def _github_login(user: dict[str, Any]) -> str | None:
    """The GitHub username when the person signs in through Auth0's GitHub connection."""
    if any(str(i.get("provider")) == "github" for i in user.get("identities") or []):
        return str(user.get("nickname") or "").strip() or None
    return None


class Auth0ManagementSource:
    name = "auth0_management"

    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        client_secret: str,
        group_prefix: str = GROUP_PREFIX,
        client: httpx.Client | None = None,
    ) -> None:
        self.domain = domain.strip().removeprefix("https://").rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.group_prefix = group_prefix
        self.client = http_client(client)
        self._token: str | None = None

    @classmethod
    def from_env(cls, config: Any = None, **kwargs: Any) -> "Auth0ManagementSource | None":
        identity = getattr(config, "identity", None)
        domain = os.environ.get("AUTH0_MGMT_DOMAIN", "").strip() or urlparse(str(getattr(identity, "issuer", "") or "")).netloc
        client_id = os.environ.get("AUTH0_MGMT_CLIENT_ID", "").strip()
        client_secret = os.environ.get("AUTH0_MGMT_CLIENT_SECRET", "").strip()
        if not (domain and client_id and client_secret):
            return None
        return cls(domain=domain, client_id=client_id, client_secret=client_secret, **kwargs)

    def _base(self) -> str:
        return f"https://{self.domain}"

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            data = check(
                self.client.post(
                    f"{self._base()}/oauth/token",
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "audience": f"{self._base()}/api/v2/",
                    },
                ),
                "Auth0 token",
            )
            self._token = str(data["access_token"])
        return {"Authorization": f"Bearer {self._token}"}

    def _pages(self, path: str, params: dict[str, Any], key: str | None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            data = check(
                self.client.get(
                    f"{self._base()}{path}",
                    params={**params, "page": page, "per_page": PER_PAGE},
                    headers=self._headers(),
                ),
                f"Auth0 {path}",
            )
            batch = data.get(key, []) if key else data
            items.extend(batch)
            if len(batch) < PER_PAGE or len(items) >= SEARCH_CEILING:
                return items
            page += 1

    def fetch(self) -> DirectorySnapshot:
        users = self._pages(
            "/api/v2/users",
            {"fields": "user_id,email,name,nickname,blocked,app_metadata,identities", "include_fields": "true", "include_totals": "true"},
            "users",
        )
        roles_by_user: dict[str, list[str]] = {}
        groups_by_user: dict[str, list[str]] = {}
        for role in self._pages("/api/v2/roles", {"include_totals": "true"}, "roles"):
            name = str(role.get("name") or "")
            members = self._pages(f"/api/v2/roles/{role['id']}/users", {"include_totals": "true"}, "users")
            target = groups_by_user if name.lower().startswith(self.group_prefix) else roles_by_user
            for member in members:
                target.setdefault(str(member.get("user_id")), []).append(name)
        people = []
        for user in users:
            subject = str(user.get("user_id") or "")
            if not subject:
                continue
            meta_groups = (user.get("app_metadata") or {}).get("groups") or []
            people.append(
                DirectoryPerson(
                    subject=subject,
                    email=lower_email(user.get("email")),
                    display_name=user.get("name"),
                    roles=sorted(roles_by_user.get(subject, [])),
                    groups=sorted({*groups_by_user.get(subject, []), *[str(g) for g in meta_groups]}),
                    disabled=bool(user.get("blocked")),
                    github_login=_github_login(user),
                )
            )
        return DirectorySnapshot(
            source=self.name,
            match="subject",
            complete=len(users) < SEARCH_CEILING,
            people=people,
        )
