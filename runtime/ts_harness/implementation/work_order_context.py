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


_REPOSITORY_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?\|?\s*\*{0,2}Repository(?:\*{0,2}\s*:|:\*{0,2}|\*{0,2}\s*\|)\s*"
    r"`?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?`?\s*(?:\||$)",
    re.IGNORECASE | re.MULTILINE,
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


def _sections(markdown: str) -> dict[str, str]:
    text = (markdown or "").strip()
    matches = list(_WO_HEADER.finditer(text))
    return {
        match.group(1).upper(): text[match.start() : matches[idx + 1].start() if idx + 1 < len(matches) else len(text)]
        for idx, match in enumerate(matches)
    }


def parse_work_order_repositories(markdown: str) -> dict[str, str]:
    """``{WO-ID: "owner/name"}`` from each section's ``Repository:`` line; sections without one are left out."""
    found: dict[str, str] = {}
    for wo_id, section in _sections(markdown).items():
        match = _REPOSITORY_LINE.search(section)
        if match:
            found[wo_id] = match.group(1)
    return found


def resolve_repository(full_name: str | None, repositories: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The project repository named ``owner/name`` (case-insensitive)."""
    wanted = str(full_name or "").strip().lower()
    for repository in repositories or []:
        if str(repository.get("full_name") or "").lower() == wanted:
            return repository
    return None


def repository_dependency_cycle(markdown: str) -> list[str] | None:
    """A cycle between repositories implied by work-order dependencies, as ``[a, b, ..., a]``."""
    repositories = {k: v.lower() for k, v in parse_work_order_repositories(markdown).items()}
    edges: dict[str, set[str]] = {}
    for wo_id, deps in parse_work_order_dependencies(markdown).items():
        source = repositories.get(wo_id)
        for dep in deps:
            target = repositories.get(dep)
            if source and target and source != target:
                edges.setdefault(source, set()).add(target)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(edges.get(node, ())):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt) :] + [nxt]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(edges):
        if node not in state:
            cycle = visit(node)
            if cycle:
                return cycle
    return None


def work_order_repository_problems(markdown: str, repositories: list[dict[str, Any]] | None) -> list[str]:
    """Why the work orders' repositories do not fit the project; empty when they do.

    With one project repository, a missing ``Repository:`` line means that one.
    With several, every work order names one of them.
    """
    if not repositories:
        return []
    named = parse_work_order_repositories(markdown)
    known = ", ".join(sorted(str(r.get("full_name")) for r in repositories))
    problems: list[str] = []
    for wo_id in _sections(markdown):
        name = named.get(wo_id)
        if name is None:
            if len(repositories) > 1:
                problems.append(f"{wo_id} has no Repository line; use one of {known}")
        elif resolve_repository(name, repositories) is None:
            problems.append(f"{wo_id} names repository {name}, which is not in the project ({known})")
    cycle = repository_dependency_cycle(markdown)
    if cycle:
        problems.append("work-order dependencies form a cycle between repositories: " + " -> ".join(cycle))
    return problems
