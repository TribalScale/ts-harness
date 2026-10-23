"""Google Workspace Directory source and group resolver.

Reuses the Google Workspace service account (``GOOGLE_WORKSPACE_SA_JSON``)
with domain-wide delegation, impersonating ``GOOGLE_DIRECTORY_ADMIN_SUBJECT``.
Group names are the local part of the group email, for example
``ts-harness-team-payments@example.com`` is ``ts-harness-team-payments``.
"""

from __future__ import annotations

import os
from typing import Any, Callable
from urllib.parse import quote

import httpx

from ts_harness.identity_directory.base import (
    GROUP_PREFIX,
    DirectoryPerson,
    DirectorySnapshot,
    check,
    follow_pages,
    http_client,
    lower_email,
)

ADMIN = "https://admin.googleapis.com/admin/directory/v1"
SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
]


def _delegated_token(admin_subject: str) -> Callable[[], str]:
    def token() -> str:
        from google.auth.transport.requests import Request

        from ts_harness.connectors.google_workspace.auth.delegation import build_delegated_credentials

        creds = build_delegated_credentials(SCOPES, admin_subject)
        creds.refresh(Request())
        return str(creds.token)

    return token


def group_name(group: dict[str, Any]) -> str:
    return str(group.get("email") or group.get("name") or "").split("@", 1)[0].lower()


class GoogleDirectorySource:
    name = "google_directory"

    def __init__(
        self,
        *,
        admin_subject: str,
        customer: str = "my_customer",
        group_prefix: str = GROUP_PREFIX,
        token_provider: Callable[[], str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.customer = customer
        self.group_prefix = group_prefix
        self.token_provider = token_provider or _delegated_token(admin_subject)
        self.client = http_client(client)
        self._token: str | None = None

    @classmethod
    def from_env(cls, config: Any = None, **kwargs: Any) -> "GoogleDirectorySource | None":
        admin_subject = os.environ.get("GOOGLE_DIRECTORY_ADMIN_SUBJECT", "").strip()
        has_sa = os.environ.get("GOOGLE_WORKSPACE_SA_JSON", "").strip() or os.environ.get(
            "GOOGLE_WORKSPACE_SA_JSON_PATH", ""
        ).strip()
        if not admin_subject or not (has_sa or kwargs.get("token_provider")):
            return None
        customer = os.environ.get("GOOGLE_DIRECTORY_CUSTOMER", "").strip() or "my_customer"
        return cls(admin_subject=admin_subject, customer=customer, **kwargs)

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token = self.token_provider()
        return {"Authorization": f"Bearer {self._token}"}

    def _all(self, url: str, params: dict[str, Any], key: str) -> list[dict[str, Any]]:
        def page(cursor: str | None) -> tuple[list[Any], str | None]:
            query = {**params, **({"pageToken": cursor} if cursor else {})}
            data = check(self.client.get(url, params=query, headers=self._headers()), "Google Directory request")
            return list(data.get(key) or []), data.get("nextPageToken")

        return list(follow_pages(page))

    def fetch(self) -> DirectorySnapshot:
        users = self._all(f"{ADMIN}/users", {"customer": self.customer, "maxResults": 500}, "users")
        groups = [
            g
            for g in self._all(f"{ADMIN}/groups", {"customer": self.customer, "maxResults": 200}, "groups")
            if group_name(g).startswith(self.group_prefix)
        ]
        groups_by_email: dict[str, list[str]] = {}
        for group in groups:
            members = self._all(
                f"{ADMIN}/groups/{quote(str(group['id']))}/members",
                {"includeDerivedMembership": "true", "maxResults": 200},
                "members",
            )
            for member in members:
                if member.get("type", "USER") == "USER" and member.get("email"):
                    groups_by_email.setdefault(str(member["email"]).lower(), []).append(group_name(group))
        people = []
        for user in users:
            email = lower_email(user.get("primaryEmail"))
            people.append(
                DirectoryPerson(
                    email=email,
                    display_name=(user.get("name") or {}).get("fullName"),
                    groups=sorted(groups_by_email.get(email or "", [])),
                    disabled=bool(user.get("suspended") or user.get("archived")),
                )
            )
        return DirectorySnapshot(source=self.name, match="email", complete=True, people=people)

    def resolve_groups(self, *, subject: str, email: str | None, claims: dict[str, Any]) -> list[str]:
        if not email:
            return []
        groups = self._all(f"{ADMIN}/groups", {"userKey": email, "maxResults": 200}, "groups")
        return sorted(n for n in (group_name(g) for g in groups) if n.startswith(self.group_prefix))
