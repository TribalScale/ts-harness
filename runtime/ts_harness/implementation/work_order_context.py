"""Extract work-order sections and dependency edges from planner markdown."""

from __future__ import annotations

import re
from typing import Any


_WO_HEADER = re.compile(r"^#{1,3}\s+(WO-\d+)\b.*$", re.IGNORECASE | re.MULTILINE)
_WO_ID = re.compile(r"\b(WO-\d+)\b", re.IGNORECASE)
_DEPENDS_ON_ROW = re.compile(
    r"\|\s*\*{0,2}Depends on\*{0,2}\s*\|\s*([^|\n]+)\|",
    re.IGNORECASE,
)


def extract_work_order_section(markdown: str, work_order_id: str) -> str:
    """Return the markdown section for ``work_order_id`` (e.g. WO-001).

    Sections are delimited by headings that start with the WO id.
    """
    text = (markdown or "").strip()
    wo_id = str(work_order_id or "").strip()
    if not text or not wo_id:
        return ""

    matches = list(_WO_HEADER.finditer(text))
    if not matches:
        return ""

    target_idx: int | None = None
    for idx, match in enumerate(matches):
        if match.group(1).upper() == wo_id.upper():
            target_idx = idx
            break
    if target_idx is None:
        return ""

    start = matches[target_idx].start()
    end = matches[target_idx + 1].start() if target_idx + 1 < len(matches) else len(text)
    return text[start:end].strip()


def parse_work_order_dependencies(markdown: str) -> dict[str, list[str]]:
    """Parse ``Depends on`` context-graph rows from work-order markdown.

    Returns ``{WO-ID: [dependency WO ids...]}``. Missing / ``None`` means no deps.
    """
    text = (markdown or "").strip()
    if not text:
        return {}

    matches = list(_WO_HEADER.finditer(text))
    deps: dict[str, list[str]] = {}
    for idx, match in enumerate(matches):
        wo_id = match.group(1).upper()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        section = text[start:end]
        row = _DEPENDS_ON_ROW.search(section)
        if not row:
            deps[wo_id] = []
            continue
        cell = row.group(1).strip()
        if not cell or cell.lower() in {"none", "n/a", "-", "—"}:
            deps[wo_id] = []
            continue
        found = [m.group(1).upper() for m in _WO_ID.finditer(cell)]
        # Exclude self-references.
        deps[wo_id] = [dep for dep in found if dep != wo_id]
    return deps


def select_next_work_order_id(
    work_order_ids: list[Any],
    completed_work_order_ids: list[Any],
    dependencies: dict[str, list[str]] | None = None,
) -> str | None:
    """Return the next unfinished WO whose dependencies are satisfied.

    Falls back to list order when no dependency map is provided.
    """
    completed = {str(wo).upper() for wo in (completed_work_order_ids or [])}
    deps = {str(k).upper(): [str(d).upper() for d in (v or [])] for k, v in (dependencies or {}).items()}
    for raw in work_order_ids or []:
        wo_id = str(raw or "").strip().upper()
        if not wo_id or wo_id in completed:
            continue
        required = deps.get(wo_id, [])
        if all(dep in completed for dep in required):
            return wo_id
    return None


DEFAULT_MAX_PARALLEL_WORK_ORDERS = 3


def max_parallel_work_orders(program_defaults: dict[str, Any] | None) -> int:
    delivery = dict((program_defaults or {}).get("delivery") or {})
    value = delivery.get("max_parallel_work_orders")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_MAX_PARALLEL_WORK_ORDERS


def select_ready_work_order_ids(
    work_order_ids: list[Any],
    completed_work_order_ids: list[Any],
    dependencies: dict[str, list[str]] | None = None,
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL_WORK_ORDERS,
) -> list[str]:
    """Next wave: unfinished WOs whose dependencies are all merged, in list order, capped.

    A WO never shares a wave with one of its dependencies, because a dependency
    only counts once it is in ``completed_work_order_ids``.
    """
    completed = {str(wo).upper() for wo in (completed_work_order_ids or [])}
    deps = {str(k).upper(): [str(d).upper() for d in (v or [])] for k, v in (dependencies or {}).items()}
    wave: list[str] = []
    for raw in work_order_ids or []:
        wo_id = str(raw or "").strip().upper()
        if not wo_id or wo_id in completed or wo_id in wave:
            continue
        if all(dep in completed for dep in deps.get(wo_id, [])):
            wave.append(wo_id)
        if len(wave) >= max(1, int(max_parallel)):
            break
    return wave


def dependencies_from_artifacts(artifacts: dict[str, Any]) -> dict[str, list[str]]:
    structured = artifacts.get("work_order_dependencies")
    if isinstance(structured, dict):
        return {str(k): [str(d) for d in (v or [])] for k, v in structured.items()}
    if artifacts.get("work_orders_markdown"):
        return parse_work_order_dependencies(str(artifacts["work_orders_markdown"]))
    return {}
