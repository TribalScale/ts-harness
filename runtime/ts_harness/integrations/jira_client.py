"""Jira Cloud REST client used by cancel compensation.

Authenticates with the same Atlassian env vars as the MCP bridge
(``ATLASSIAN_SITE_URL``/``JIRA_URL``, ``ATLASSIAN_EMAIL``/``JIRA_USERNAME``,
``ATLASSIAN_API_TOKEN``/``JIRA_API_TOKEN``). The transport is injectable so tests
never touch the network.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

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
    site_url: str
    email: str
    token: str
    transport: Transport = _urllib_transport

    @classmethod
    def from_env(cls, *, transport: Transport | None = None) -> "JiraClient":
        site = (os.environ.get("ATLASSIAN_SITE_URL") or os.environ.get("JIRA_URL") or "").strip().rstrip("/")
        email = (os.environ.get("ATLASSIAN_EMAIL") or os.environ.get("JIRA_USERNAME") or "").strip()
        token = (os.environ.get("ATLASSIAN_API_TOKEN") or os.environ.get("JIRA_API_TOKEN") or "").strip()
        if not (site and email and token):
            raise JiraError(401, "ATLASSIAN_SITE_URL, ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN are required")
        return cls(site_url=site, email=email, token=token, transport=transport or _urllib_transport)

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> Any:
        credentials = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "User-Agent": "ts-harness",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self.transport(method, f"{self.site_url}{path}", headers, data)
        if status >= 400:
            raise JiraError(status, raw.decode(errors="replace") if raw else "")
        if not raw:
            return {}
        return json.loads(raw.decode())

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
