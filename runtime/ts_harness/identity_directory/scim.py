"""SCIM source: the people and groups Entra ID or Okta pushed to ``/scim/v2``."""

from __future__ import annotations

from typing import Any

from ts_harness.identity_directory.base import DirectoryError, DirectoryPerson, DirectorySnapshot, lower_email


class ScimSource:
    name = "scim"

    def __init__(self, *, memory: Any, tenant_id: str = "default") -> None:
        self.memory = memory
        self.tenant_id = tenant_id

    def fetch(self) -> DirectorySnapshot:
        status, data = self.memory.scim_request("GET", "snapshot", tenant_id=self.tenant_id)
        if status >= 400:
            raise DirectoryError(f"SCIM snapshot failed: HTTP {status}")
        people = [
            DirectoryPerson(
                email=lower_email(user.get("email")),
                display_name=user.get("display_name"),
                roles=list(user.get("roles") or []) or None,
                groups=list(user.get("groups") or []),
                disabled=bool(user.get("disabled")),
            )
            for user in data.get("users") or []
        ]
        return DirectorySnapshot(source=self.name, match="email", complete=True, people=people)
