"""Jira Cloud REST client used by cancel compensation.

Authenticates through ``AtlassianAuth``, the same credentials as the MCP bridge:
a service account OAuth credential, or an email and API token. The transport is
injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ts_harness.integrations.atlassian_auth import AtlassianAuth, AtlassianAuthError
from ts_harness.integrations.github_client import Transport, _urllib_transport


class JiraError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Jira API error ({status}): {message[:300]}")
        self.status = status


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


@dataclass
class JiraClient:
    auth: AtlassianAuth
    transport: Transport = _urllib_transport

    @classmethod
    def from_env(cls, *, transport: Transport | None = None) -> "JiraClient":
        try:
            auth = AtlassianAuth.from_env(transport=transport)
        except AtlassianAuthError as exc:
            raise JiraError(exc.status, str(exc)) from exc
        return cls(auth=auth, transport=transport or _urllib_transport)

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        status, raw = self._send(method, path, data)
        if status == 401 and self.auth.mode == "oauth":
            self.auth.invalidate()
            status, raw = self._send(method, path, data)
        if status >= 400:
            raise JiraError(status, raw.decode(errors="replace") if raw else "")
        if not raw:
            return {}
        return json.loads(raw.decode())

    def _send(self, method: str, path: str, data: bytes | None) -> tuple[int, bytes]:
        try:
            headers = {
                "Authorization": self.auth.authorization(),
                "Accept": "application/json",
                "User-Agent": "ts-harness",
            }
            url = f"{self.auth.base_url()}{path}"
        except AtlassianAuthError as exc:
            raise JiraError(exc.status, str(exc)) from exc
        if data is not None:
            headers["Content-Type"] = "application/json"
        response = self.transport(method, url, headers, data)
        return response[0], response[1]

    def _issue_path(self, key: str) -> str:
        return f"/rest/api/3/issue/{urllib.parse.quote(key, safe='')}"

    def add_label(self, key: str, label: str) -> None:
        self.request("PUT", self._issue_path(key), body={"update": {"labels": [{"add": label}]}})

    def comment(self, key: str, text: str) -> None:
        self.request("POST", f"{self._issue_path(key)}/comment", body={"body": _adf(text)})

    def transition(self, key: str, name: str) -> bool:
        """Apply the transition named ``name``; ``False`` when the issue has no such transition."""
        payload = self.request("GET", f"{self._issue_path(key)}/transitions") or {}
        wanted = name.strip().lower()
        for option in payload.get("transitions") or []:
            names = {str(option.get("name") or "").lower(), str((option.get("to") or {}).get("name") or "").lower()}
            if wanted in names:
                self.request("POST", f"{self._issue_path(key)}/transitions", body={"transition": {"id": option["id"]}})
                return True
        return False
