"""Legacy skill ids that still resolve to their current names."""

from __future__ import annotations

from typing import Iterable

LEGACY_SKILL_ALIASES: dict[str, str] = {
    "ts-prd-to-work-orders": "prd-to-work-orders",
    "ts-prd-to-work-orders-governed": "prd-to-work-orders-governed",
    "ts-source-work-order": "source-work-order",
    "ts-source-work-order-governed": "source-work-order-governed",
    "ts-verified-merge": "verified-merge",
    "ts-verified-merge-governed": "verified-merge-governed",
}


def canonical_skill_id(skill_id: str) -> str:
    return LEGACY_SKILL_ALIASES.get(skill_id, skill_id)


def canonical_skill_ids(skill_ids: Iterable[object] | None) -> list[str]:
    return [canonical_skill_id(str(item)) for item in (skill_ids or [])]
