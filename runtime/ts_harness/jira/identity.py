"""Map a requester email to a Jira account id. Never match display names."""

from __future__ import annotations

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
