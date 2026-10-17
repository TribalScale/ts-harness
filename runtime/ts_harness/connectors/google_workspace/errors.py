"""Structured errors for Google Workspace connector."""

from __future__ import annotations

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def format_delegation_error(err: Exception) -> dict[str, str]:
    msg = str(err)
    base = {"error": msg, "scope_required": GMAIL_READONLY_SCOPE}
    if "unauthorized_client" in msg.lower() or "not authorized" in msg.lower():
        return {
            **base,
            "hint": (
                "Workspace domain-wide delegation may be missing or Gmail API disabled. "
                "Authorize the service account client ID with required scopes in Google Admin."
            ),
        }
    return {
        **base,
        "hint": "Ensure Gmail/Calendar/Drive APIs are enabled and delegation scopes match config.",
    }
