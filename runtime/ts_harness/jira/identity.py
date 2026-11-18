"""Map a requester email to a Jira account id. Never match display names."""

from __future__ import annotations

from typing import Any

UNMAPPED = "unmapped"


def resolve_jira_query_user(
    slack_email: str | None,
    mapping: dict[str, str] | None,
) -> str:
    email = str(slack_email or "").strip().lower()
    if not email:
        return UNMAPPED
    for raw_key, raw_account in (mapping or {}).items():
        key = str(raw_key or "").strip().lower()
        account = str(raw_account or "").strip()
        if key == email and account:
            return account
    return UNMAPPED


def lookup_jira_account(jira: Any, email: str) -> str:
    """The Jira account whose email is ``email``, by Jira's user search; ``UNMAPPED`` if none.

    Jira matches the query against email and name. An account showing its email must match it
    exactly; when Jira hides emails, a single active human account found by the full email is
    taken. Display names are never matched on their own.
    """
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        return UNMAPPED
    from urllib.parse import quote

    try:
        users = jira.request("GET", f"/rest/api/3/user/search?query={quote(email)}&maxResults=10") or []
    except Exception:  # noqa: BLE001
        return UNMAPPED
    humans = [u for u in users if isinstance(u, dict) and u.get("accountType", "atlassian") == "atlassian" and u.get("active", True)]
    exact = [u for u in humans if str(u.get("emailAddress") or "").lower() == email]
    if exact:
        return str(exact[0].get("accountId") or UNMAPPED)
    hidden = [u for u in humans if not u.get("emailAddress")]
    if len(humans) == 1 and hidden:
        return str(hidden[0].get("accountId") or UNMAPPED)
    return UNMAPPED


def jira_account_for_email(email: str | None, mapping: dict[str, str] | None, jira: Any = None) -> str:
    """Settings mapping (``identity.jira_account_ids``) first, then Jira's user search."""
    account = resolve_jira_query_user(email, mapping)
    if account != UNMAPPED or jira is None:
        return account
    return lookup_jira_account(jira, str(email or ""))
