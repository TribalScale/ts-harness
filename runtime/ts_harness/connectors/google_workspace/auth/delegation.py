"""Service-account domain-wide delegation auth."""

from __future__ import annotations

import json
import os
from typing import Any

_cached_credentials: dict[str, Any] | None = None


def _load_sa_info() -> dict[str, Any]:
    global _cached_credentials
    if _cached_credentials is not None:
        return _cached_credentials
    raw = os.environ.get("GOOGLE_WORKSPACE_SA_JSON", "").strip()
    if not raw:
        path = os.environ.get("GOOGLE_WORKSPACE_SA_JSON_PATH", "").strip()
        if path and os.path.isfile(path):
            raw = open(path, encoding="utf-8").read()
    if not raw:
        raise RuntimeError(
            "GOOGLE_WORKSPACE_SA_JSON or GOOGLE_WORKSPACE_SA_JSON_PATH must be set for delegation"
        )
    _cached_credentials = json.loads(raw)
    if _cached_credentials.get("private_key"):
        _cached_credentials["private_key"] = (
            str(_cached_credentials["private_key"]).replace("\\n", "\n").replace('""', '"')
        )
    return _cached_credentials


def build_delegated_credentials(scopes: list[str], subject: str):
    from google.oauth2 import service_account

    info = _load_sa_info()
    return service_account.Credentials.from_service_account_info(
        info,
        scopes=scopes,
        subject=subject,
    )


def probe_delegation(scopes: list[str], subject: str) -> tuple[bool, str | None]:
    if os.environ.get("TS_HARNESS_SKIP_GOOGLE_PROBE", "").lower() == "true":
        return True, None
    try:
        creds = build_delegated_credentials(scopes, subject)
        from googleapiclient.discovery import build

        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        service.calendarList().list(maxResults=1).execute()
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:240]
