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


MAX_BULK_CREATE = 50
_BLOCK_NODES = {"paragraph", "heading", "codeBlock", "blockquote", "listItem", "tableRow", "rule", "panel"}


def adf_from_text(text: str) -> dict[str, Any]:
    """Plain text as an ADF document: blank lines split paragraphs, newlines become hard breaks."""
    paragraphs = [part for part in str(text or "").replace("\r\n", "\n").split("\n\n") if part.strip()]
    content = []
    for paragraph in paragraphs:
        nodes: list[dict[str, Any]] = []
        for index, line in enumerate(paragraph.split("\n")):
            if index:
                nodes.append({"type": "hardBreak"})
            if line:
                nodes.append({"type": "text", "text": line})
        content.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind in {"mention", "emoji"}:
        return str((node.get("attrs") or {}).get("text") or "")
    if kind == "tableCell" or kind == "tableHeader":
        return " | ".join(adf_to_text(child).strip() for child in node.get("content") or []) + " | "
    inner = "".join(adf_to_text(child) for child in node.get("content") or [])
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind == "tableRow":
        return f"| {inner.rstrip()}\n"
    if kind in _BLOCK_NODES:
        return f"{inner.rstrip()}\n\n"
    return inner



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
        self.request("POST", f"{self._issue_path(key)}/comment", body={"body": adf_from_text(text)})

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

    def search(self, jql: str, *, max_results: int = 10, fields: list[str] | None = None) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {
                "jql": jql,
                "maxResults": str(max(1, min(int(max_results), 100))),
                "fields": ",".join(fields or ["summary", "status", "issuetype", "assignee"]),
            }
        )
        payload = self.request("GET", f"/rest/api/3/search/jql?{params}") or {}
        return list(payload.get("issues") or [])

    def get_issue(self, key: str) -> dict[str, Any]:
        fields = "summary,status,issuetype,description,parent,labels,priority,assignee,issuelinks"
        return self.request("GET", f"{self._issue_path(key)}?fields={fields}") or {}

    def list_projects(self, *, action: str = "browse") -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"action": action, "maxResults": "50", "orderBy": "key"})
        payload = self.request("GET", f"/rest/api/3/project/search?{params}") or {}
        return list(payload.get("values") or [])

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/rest/api/3/issue", body={"fields": fields}) or {}

    def bulk_create_issues(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        if len(issues) > MAX_BULK_CREATE:
            raise JiraError(400, f"at most {MAX_BULK_CREATE} issues per bulk create")
        body = {"issueUpdates": [{"fields": fields} for fields in issues]}
        return self.request("POST", "/rest/api/3/issue/bulk", body=body) or {}

    def link_issues(self, link_type: str, *, inward_key: str, outward_key: str) -> None:
        body = {"type": {"name": link_type}, "inwardIssue": {"key": inward_key}, "outwardIssue": {"key": outward_key}}
        self.request("POST", "/rest/api/3/issueLink", body=body)

    def browse_url(self, key: str) -> str:
        site = self.auth.site_url
        return f"{site}/browse/{key}" if site else ""
