"""Admin BFF for the read-only skills catalog."""

from __future__ import annotations

from typing import Any

from ts_harness.skills.catalog_view import get_skill_catalog_item, list_skill_catalog


def list_skills_catalog_admin(runtime: Any) -> dict[str, Any]:
    return list_skill_catalog(runtime)


def get_skills_catalog_item_admin(runtime: Any, skill_id: str) -> dict[str, Any]:
    return get_skill_catalog_item(runtime, skill_id)
