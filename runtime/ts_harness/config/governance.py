"""Tenant governance settings that are pinned onto each program at creation."""

from __future__ import annotations

from typing import Any

INDEPENDENT_REVIEW_DEFAULT = True


def governance_snapshot(program_defaults: dict[str, Any] | None) -> dict[str, Any]:
    """Return the effective governance values; missing keys use safe defaults."""
    governance = dict((program_defaults or {}).get("governance") or {})
    review = governance.get("independent_review")
    enabled = INDEPENDENT_REVIEW_DEFAULT
    if isinstance(review, dict) and isinstance(review.get("enabled"), bool):
        enabled = review["enabled"]
    return {"independent_review": {"enabled": enabled}}


def independent_review_enabled(snapshot: dict[str, Any] | None) -> bool:
    review = dict((snapshot or {}).get("independent_review") or {})
    value = review.get("enabled")
    return value if isinstance(value, bool) else INDEPENDENT_REVIEW_DEFAULT


def program_governance_snapshot(program: dict[str, Any] | None) -> dict[str, Any]:
    """Read the pinned snapshot from a program row; default-on when absent."""
    metadata = (program or {}).get("metadata") or {}
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    pinned = metadata.get("governance_snapshot") if isinstance(metadata, dict) else None
    if isinstance(pinned, dict):
        return {"independent_review": {"enabled": independent_review_enabled(pinned)}}
    return governance_snapshot({})
