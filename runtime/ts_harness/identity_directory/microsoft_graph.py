"""Microsoft Graph source and group resolver for Entra ID.

Needs an app registration with ``User.Read.All`` and ``GroupMember.Read.All``
application permissions. People are matched to sign-ins by email (``mail``,
falling back to ``userPrincipalName``).
"""

from __future__ import annotations

import os
from typing import Any
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

GRAPH = "https://graph.microsoft.com/v1.0"


class MicrosoftGraphSource:
    name = "microsoft_graph"

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        group_prefix: str = GROUP_PREFIX,
        client: httpx.Client | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.group_prefix = group_prefix
        self.client = http_client(client)
        self._token: str | None = None

    @classmethod
    def from_env(cls, config: Any = None, **kwargs: Any) -> "MicrosoftGraphSource | None":
        tenant_id = os.environ.get("MS_GRAPH_TENANT_ID", "").strip()
        client_id = os.environ.get("MS_GRAPH_CLIENT_ID", "").strip()
        client_secret = os.environ.get("MS_GRAPH_CLIENT_SECRET", "").strip()
        if not (tenant_id and client_id and client_secret):
            return None
        return cls(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret, **kwargs)

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            data = check(
                self.client.post(
                    f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    },
                ),
                "Graph token",
            )
            self._token = str(data["access_token"])
        return {"Authorization": f"Bearer {self._token}"}

    def _all(self, url: str) -> list[dict[str, Any]]:
        def page(cursor: str | None) -> tuple[list[Any], str | None]:
            data = check(self.client.get(cursor or url, headers=self._headers()), "Graph request")
            return list(data.get("value") or []), data.get("@odata.nextLink")

        return list(follow_pages(page))

    def fetch(self) -> DirectorySnapshot:
        users = self._all(f"{GRAPH}/users?$select=id,mail,userPrincipalName,displayName,accountEnabled&$top=999")
        groups = self._all(
            f"{GRAPH}/groups?$filter=startswith(displayName,'{self.group_prefix}')&$select=id,displayName&$top=999"
        )
        groups_by_user: dict[str, list[str]] = {}
        for group in groups:
            members = self._all(f"{GRAPH}/groups/{group['id']}/transitiveMembers/microsoft.graph.user?$select=id&$top=999")
            for member in members:
                groups_by_user.setdefault(str(member["id"]), []).append(str(group["displayName"]))
        people = [
            DirectoryPerson(
                email=lower_email(user.get("mail") or user.get("userPrincipalName")),
                display_name=user.get("displayName"),
                groups=sorted(groups_by_user.get(str(user["id"]), [])),
                disabled=user.get("accountEnabled") is False,
            )
            for user in users
        ]
        return DirectorySnapshot(source=self.name, match="email", complete=True, people=people)

    def resolve_groups(self, *, subject: str, email: str | None, claims: dict[str, Any]) -> list[str]:
        if not email:
            return []
        url = f"{GRAPH}/users/{quote(email)}/transitiveMemberOf/microsoft.graph.group?$select=displayName&$top=999"
        names = [str(g.get("displayName") or "") for g in self._all(url)]
        return sorted(n for n in names if n.lower().startswith(self.group_prefix))
