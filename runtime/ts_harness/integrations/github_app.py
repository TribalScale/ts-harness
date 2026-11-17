"""GitHub App authentication: per-repository installation tokens.

One App registration per GitHub host. ``github.com`` comes from the
environment (``GITHUB_APP_ID`` plus ``GITHUB_APP_PRIVATE_KEY`` or
``GITHUB_APP_PRIVATE_KEY_SECRET``); other hosts (Enterprise Server) come from
``github_app_registrations`` with the key in the secret store. Without a
registration, callers fall back to the ``GITHUB_TOKEN`` PAT.

Tokens are minted for exactly one repository and cached until five minutes
before they expire.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

DEFAULT_HOST = "github.com"
TOKEN_REFRESH_MARGIN = 300
INSTALLATION_TTL = 3600
MISSING_INSTALLATION_TTL = 60
REGISTRATION_TTL = 300

Transport = Callable[[str, str, dict[str, str], bytes | None], Any]


class GitHubAppError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub App error ({status}): {message[:300]}")
        self.status = status


class NotInstalled(GitHubAppError):
    """The App is not installed on the repository's account, or not granted that repository."""


def api_base_for_host(host: str) -> str:
    host = (host or DEFAULT_HOST).strip().lower()
    if host in {"github.com", "www.github.com", "api.github.com"}:
        return "https://api.github.com"
    return f"https://{host}/api/v3"


def app_jwt(app_id: str, private_key: str, *, now: float | None = None) -> str:
    import jwt

    issued = int(now if now is not None else time.time()) - 60
    return jwt.encode({"iat": issued, "exp": issued + 540, "iss": str(app_id)}, private_key, algorithm="RS256")


def _expires_at(value: str) -> float:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


@dataclass
class GitHubApp:
    app_id: str
    private_key: str
    host: str = DEFAULT_HOST
    transport: Transport | None = None
    clock: Callable[[], float] = time.time
    _installations: dict[tuple[str, str], tuple[int | None, float]] = field(default_factory=dict)
    _tokens: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def api_base(self) -> str:
        return api_base_for_host(self.host)

    def _call(self, method: str, path: str, *, bearer: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        from ts_harness.integrations.github_client import USER_AGENT, _urllib_transport

        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        response = (self.transport or _urllib_transport)(method, f"{self.api_base}{path}", headers, data)
        status, raw = response[0], response[1]
        payload = json.loads(raw.decode()) if raw else {}
        return status, payload

    def jwt(self) -> str:
        return app_jwt(self.app_id, self.private_key, now=self.clock())

    def installation_id(self, owner: str, repo: str) -> int | None:
        key = (owner.lower(), repo.lower())
        now = self.clock()
        cached = self._installations.get(key)
        if cached and cached[1] > now:
            return cached[0]
        status, payload = self._call("GET", f"/repos/{owner}/{repo}/installation", bearer=self.jwt())
        if status == 404:
            self._installations[key] = (None, now + MISSING_INSTALLATION_TTL)
            return None
        if status >= 400:
            raise GitHubAppError(status, str(payload))
        installation = int(payload["id"])
        self._installations[key] = (installation, now + INSTALLATION_TTL)
        return installation

    def repo_token(self, owner: str, repo: str) -> str:
        key = (owner.lower(), repo.lower())
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] - TOKEN_REFRESH_MARGIN > self.clock():
                return cached[0]
            token, expires = self._mint(owner, repo)
            self._tokens[key] = (token, expires)
            return token

    def repo_token_with_expiry(self, owner: str, repo: str) -> tuple[str, float]:
        token = self.repo_token(owner, repo)
        return token, self._tokens[(owner.lower(), repo.lower())][1]

    def _mint(self, owner: str, repo: str) -> tuple[str, float]:
        installation = self.installation_id(owner, repo)
        if installation is None:
            raise NotInstalled(404, f"the GitHub App is not installed for {owner}/{repo} on {self.host}")
        status, payload = self._call(
            "POST",
            f"/app/installations/{installation}/access_tokens",
            bearer=self.jwt(),
            body={"repositories": [repo]},
        )
        if status == 422 or status == 404:
            self._installations.pop((owner.lower(), repo.lower()), None)
            raise NotInstalled(status, f"the GitHub App installation does not include {owner}/{repo}")
        if status >= 400:
            raise GitHubAppError(status, str(payload))
        return str(payload["token"]), _expires_at(payload["expires_at"])

    def installation_token(self, installation_id: int) -> str:
        status, payload = self._call("POST", f"/app/installations/{installation_id}/access_tokens", bearer=self.jwt())
        if status >= 400:
            raise GitHubAppError(status, str(payload))
        return str(payload["token"])

    def installation_rate_limit(self, installation_id: int) -> dict[str, int]:
        """Core REST rate limit of one installation. ``GET /rate_limit`` does not count against it."""
        key = ("#installation", str(installation_id))
        with self._lock:
            cached = self._tokens.get(key)
            if not cached or cached[1] - TOKEN_REFRESH_MARGIN <= self.clock():
                status, payload = self._call(
                    "POST", f"/app/installations/{installation_id}/access_tokens", bearer=self.jwt()
                )
                if status >= 400:
                    raise GitHubAppError(status, str(payload))
                cached = (str(payload["token"]), _expires_at(payload["expires_at"]))
                self._tokens[key] = cached
        status, payload = self._call("GET", "/rate_limit", bearer=cached[0])
        if status >= 400:
            raise GitHubAppError(status, str(payload))
        core = (payload.get("resources") or {}).get("core") or payload.get("rate") or {}
        return {"remaining": int(core["remaining"]), "limit": int(core["limit"]), "reset": int(core.get("reset") or 0)}

    def list_installations(self) -> list[dict[str, Any]]:
        status, payload = self._call("GET", "/app/installations?per_page=100", bearer=self.jwt())
        if status >= 400:
            raise GitHubAppError(status, str(payload))
        return list(payload or [])

    def installation_repositories(self, installation_id: int) -> list[str]:
        token = self.installation_token(installation_id)
        names: list[str] = []
        for page in range(1, 11):
            status, payload = self._call(
                "GET", f"/installation/repositories?per_page=100&page={page}", bearer=token
            )
            if status >= 400:
                raise GitHubAppError(status, str(payload))
            batch = [str(r.get("full_name")) for r in (payload or {}).get("repositories") or []]
            names += batch
            if len(batch) < 100:
                break
        return names


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------


def _secret_value(ref: str) -> str:
    import boto3

    region = os.environ.get("CLOUD_REGION") or os.environ.get("AWS_REGION") or None
    return str(boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=ref).get("SecretString") or "")


@dataclass
class Registration:
    host: str
    app_id: str
    private_key: str
    webhook_secret: str = ""
    app_slug: str = ""


_apps: dict[str, tuple[GitHubApp | None, Registration | None, float]] = {}
_apps_lock = threading.Lock()


def _env_registration(host: str) -> Registration | None:
    env_host = (os.environ.get("GITHUB_APP_HOST") or DEFAULT_HOST).strip().lower()
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    if not app_id or host != env_host:
        return None
    key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not key and os.environ.get("GITHUB_APP_PRIVATE_KEY_SECRET", "").strip():
        key = _secret_value(os.environ["GITHUB_APP_PRIVATE_KEY_SECRET"].strip())
    if not key:
        return None
    return Registration(
        host=host,
        app_id=app_id,
        private_key=key.replace("\\n", "\n"),
        webhook_secret=os.environ.get("GITHUB_APP_WEBHOOK_SECRET", "").strip(),
        app_slug=os.environ.get("GITHUB_APP_SLUG", "").strip(),
    )


def _stored_registration(host: str) -> Registration | None:
    try:
        from ts_harness.memory.client import get_memory_client

        row = get_memory_client().get_github_registration(host)
    except Exception:  # noqa: BLE001
        return None
    if not row or not row.get("app_id") or not row.get("private_key_secret_arn"):
        return None
    secret = row.get("webhook_secret_arn")
    return Registration(
        host=host,
        app_id=str(row["app_id"]),
        private_key=_secret_value(row["private_key_secret_arn"]),
        webhook_secret=_secret_value(secret) if secret else "",
        app_slug=str(row.get("app_slug") or ""),
    )


def registration_for(host: str = DEFAULT_HOST) -> Registration | None:
    return _resolve(host)[1]


def app_for(host: str = DEFAULT_HOST) -> GitHubApp | None:
    return _resolve(host)[0]


def _resolve(host: str) -> tuple[GitHubApp | None, Registration | None]:
    host = (host or DEFAULT_HOST).strip().lower()
    now = time.time()
    with _apps_lock:
        cached = _apps.get(host)
        if cached and cached[2] > now:
            return cached[0], cached[1]
    registration = _env_registration(host) or _stored_registration(host)
    previous = cached[0] if cached else None
    app = None
    if registration:
        same = previous and previous.app_id == registration.app_id and previous.private_key == registration.private_key
        app = previous if same else GitHubApp(registration.app_id, registration.private_key, host=host)
    with _apps_lock:
        _apps[host] = (app, registration, now + REGISTRATION_TTL)
    return app, registration


def reset_cache() -> None:
    with _apps_lock:
        _apps.clear()
