"""OAuth consent URL and token exchange."""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import urlencode

import httpx

DEFAULT_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/documents",
]


def oauth_client_config() -> tuple[str, str, str] | None:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    callback = os.environ.get("GOOGLE_OAUTH_CALLBACK_URL", "").strip()
    if client_id and client_secret and callback:
        return client_id, client_secret, callback
    return None


def build_state(*, tenant_id: str, email: str, return_url: str = "") -> str:
    payload = {"tenant_id": tenant_id, "email": email, "return_url": return_url}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def parse_state(state: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(state.encode()).decode())


def build_consent_url(*, tenant_id: str, email: str, scopes: list[str] | None = None) -> str:
    cfg = oauth_client_config()
    if not cfg:
        raise RuntimeError("Google OAuth is not configured")
    client_id, _, callback = cfg
    scope_list = scopes or DEFAULT_SCOPES
    params = {
        "client_id": client_id,
        "redirect_uri": callback,
        "response_type": "code",
        "scope": " ".join(scope_list),
        "access_type": "offline",
        "prompt": "consent",
        "state": build_state(tenant_id=tenant_id, email=email),
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    cfg = oauth_client_config()
    if not cfg:
        raise RuntimeError("Google OAuth is not configured")
    client_id, client_secret, callback = cfg
    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": callback,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_google_email(access_token: str) -> str | None:
    try:
        resp = httpx.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("email")
    except Exception:
        return None
