"""Atlassian Cloud authentication shared by the Jira client and the MCP bridge.

Two modes, chosen from the environment:

* **oauth**: an Atlassian service account's OAuth 2.0 credential
  (``ATLASSIAN_CLIENT_ID`` and ``ATLASSIAN_CLIENT_SECRET``). Access tokens come
  from the client credentials grant and are cached until shortly before they
  expire. Requests go through ``https://api.atlassian.com/ex/jira/<cloudId>``;
  the cloud id is ``ATLASSIAN_CLOUD_ID`` or read once from the site's
  ``/_edge/tenant_info``.
* **basic**: an account email and API token (``ATLASSIAN_EMAIL``/``JIRA_USERNAME``
  and ``ATLASSIAN_API_TOKEN``/``JIRA_API_TOKEN``) sent to the site URL.

OAuth wins when both are configured. The transport is injectable so tests never
touch the network.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

TOKEN_URL = "https://auth.atlassian.com/oauth/token"
API_GATEWAY = "https://api.atlassian.com/ex/jira"
TOKEN_REFRESH_MARGIN = 60
DEFAULT_TOKEN_TTL = 3600
USER_AGENT = "ts-harness"

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple]


class AtlassianAuthError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Atlassian auth error ({status}): {message[:300]}")
        self.status = status


def _env(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def normalize_site(site: str) -> str:
    site = (site or "").strip().rstrip("/")
    if not site:
        return ""
    if site.startswith("http://") or site.startswith("https://"):
        return site
    return f"https://{site}"


def _default_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple:
    from ts_harness.integrations.github_client import _urllib_transport

    return _urllib_transport(method, url, headers, body)


@dataclass
class AtlassianAuth:
    mode: str
    site_url: str = ""
    email: str = ""
    api_token: str = field(default="", repr=False)
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    cloud_id: str = ""
    transport: Transport = _default_transport
    clock: Callable[[], float] = time.time
    _token: tuple[str, float] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_env(cls, *, transport: Transport | None = None, clock: Callable[[], float] | None = None) -> "AtlassianAuth":
        site = normalize_site(_env("ATLASSIAN_SITE_URL", "JIRA_URL"))
        client_id = _env("ATLASSIAN_CLIENT_ID")
        client_secret = _env("ATLASSIAN_CLIENT_SECRET")
        extra: dict[str, Any] = {}
        if transport is not None:
            extra["transport"] = transport
        if clock is not None:
            extra["clock"] = clock
        if client_id and client_secret:
            cloud_id = _env("ATLASSIAN_CLOUD_ID")
            if not cloud_id and not site:
                raise AtlassianAuthError(401, "ATLASSIAN_CLOUD_ID or ATLASSIAN_SITE_URL is required with ATLASSIAN_CLIENT_ID")
            return cls(
                mode="oauth",
                site_url=site,
                client_id=client_id,
                client_secret=client_secret,
                cloud_id=cloud_id,
                **extra,
            )
        email = _env("ATLASSIAN_EMAIL", "JIRA_USERNAME")
        token = _env("ATLASSIAN_API_TOKEN", "JIRA_API_TOKEN")
        if site and email and token:
            return cls(mode="basic", site_url=site, email=email, api_token=token, **extra)
        raise AtlassianAuthError(
            401,
            "Jira credentials are required: ATLASSIAN_CLIENT_ID and ATLASSIAN_CLIENT_SECRET (service account), "
            "or ATLASSIAN_SITE_URL, ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN",
        )

    def base_url(self) -> str:
        if self.mode == "basic":
            return self.site_url
        return f"{API_GATEWAY}/{self._cloud_id()}"

    def authorization(self) -> str:
        if self.mode == "basic":
            raw = f"{self.email}:{self.api_token}".encode()
            return "Basic " + base64.b64encode(raw).decode()
        return f"Bearer {self._access_token()}"

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def _call(self, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        response = self.transport(method, url, headers, data)
        status, raw = response[0], response[1]
        try:
            payload = json.loads(raw.decode()) if raw else {}
        except ValueError:
            payload = {}
        return status, payload

    def _access_token(self) -> str:
        with self._lock:
            cached = self._token
            if cached and cached[1] - TOKEN_REFRESH_MARGIN > self.clock():
                return cached[0]
            status, payload = self._call(
                "POST",
                TOKEN_URL,
                {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            )
            token = str((payload or {}).get("access_token") or "") if isinstance(payload, dict) else ""
            if status >= 400 or not token:
                reason = payload.get("error_description") or payload.get("error") if isinstance(payload, dict) else ""
                raise AtlassianAuthError(status if status >= 400 else 502, f"token request failed: {reason or 'no access_token'}")
            ttl = int(payload.get("expires_in") or DEFAULT_TOKEN_TTL)
            self._token = (token, self.clock() + ttl)
            return token

    def _cloud_id(self) -> str:
        if self.cloud_id:
            return self.cloud_id
        with self._lock:
            if self.cloud_id:
                return self.cloud_id
            status, payload = self._call("GET", f"{self.site_url}/_edge/tenant_info")
            cloud_id = str((payload or {}).get("cloudId") or "") if isinstance(payload, dict) else ""
            if status >= 400 or not cloud_id:
                raise AtlassianAuthError(status if status >= 400 else 502, f"could not read the cloud id from {self.site_url}")
            self.cloud_id = cloud_id
            return cloud_id
