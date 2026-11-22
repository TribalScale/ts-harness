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
# The planner template writes the Context Graph as bullets: "- Depends on: WO-001, WO-002".
_DEPENDS_ON_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}Depends on\*{0,2}\s*:\s*\*{0,2}\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


_REPOSITORY_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?\|?\s*\*{0,2}Repository(?:\*{0,2}\s*:|:\*{0,2}|\*{0,2}\s*\|)\s*"
    r"`?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?`?\s*(?:\||$)",
    re.IGNORECASE | re.MULTILINE,
)
_PLACEHOLDER_REPOSITORY = "owner/name"


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
        row = _DEPENDS_ON_ROW.search(section) or _DEPENDS_ON_LINE.search(section)
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
        deps = {str(k): [str(d) for d in (v or [])] for k, v in structured.items()}
    elif artifacts.get("work_orders_markdown"):
        deps = parse_work_order_dependencies(str(artifacts["work_orders_markdown"]))
    else:
        deps = {}
    return with_onboarding_gates(deps, artifacts)


def with_onboarding_gates(deps: dict[str, list[str]], artifacts: dict[str, Any]) -> dict[str, list[str]]:
    """Make each work order depend on its repository's onboarding work order (``onboarding_gates``).

    A gate names ``owner/name``, or ``""`` when the program has one repository. Work orders that
    don't name a repository wait for every gate.
    """
    gates = {str(k).upper(): str(v or "").lower() for k, v in (artifacts.get("onboarding_gates") or {}).items()}
    if not gates:
        return deps
    named = {k: v.lower() for k, v in parse_work_order_repositories(str(artifacts.get("work_orders_markdown") or "")).items()}
    ids = {str(wo).upper() for wo in artifacts.get("work_order_ids") or []} | {str(k).upper() for k in deps}
    out = {str(k).upper(): list(v) for k, v in deps.items()}
    for wo_id in ids - set(gates):
        repo = named.get(wo_id, "")
        for gate, gate_repo in gates.items():
            if (not gate_repo or not repo or gate_repo == repo) and gate not in out.setdefault(wo_id, []):
                out[wo_id].append(gate)
    return out


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
    With several, every work order names one of them. The template placeholder
    ``owner/name`` is never a repository.
    """
    named = parse_work_order_repositories(markdown)
    problems = [
        f"{wo_id} has the template placeholder Repository: {name}; name a real repository or leave the line out"
        for wo_id, name in named.items()
        if name.lower() == _PLACEHOLDER_REPOSITORY
    ]
    if not repositories:
        return problems
    known = ", ".join(sorted(str(r.get("full_name")) for r in repositories))
    for wo_id in _sections(markdown):
        name = named.get(wo_id)
        if name is not None and name.lower() == _PLACEHOLDER_REPOSITORY:
            continue
        if name is None:
            if len(repositories) > 1:
                problems.append(f"{wo_id} has no Repository line; use one of {known}")
        elif resolve_repository(name, repositories) is None:
            problems.append(f"{wo_id} names repository {name}, which is not in the project ({known})")
    cycle = repository_dependency_cycle(markdown)
    if cycle:
        problems.append("work-order dependencies form a cycle between repositories: " + " -> ".join(cycle))
    return problems


# Delivery plan: how the work orders will run in parallel, and which look too large.
LARGE_WORK_ORDER_FILES = 15
LARGE_WORK_ORDER_REQUIREMENTS = 12
_HEADING = re.compile(r"^#{2,6}\s*(.+?)\s*$", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)


def _section_bullets(section: str, names: set[str]) -> int:
    headings = list(_HEADING.finditer(section))
    for idx, match in enumerate(headings):
        if match.group(1).strip().lower() in names:
            end = headings[idx + 1].start() if idx + 1 < len(headings) else len(section)
            return len(_BULLET.findall(section[match.end():end]))
    return 0


def delivery_waves(deps: dict[str, list[str]]) -> list[list[str]]:
    """Work orders grouped by when they can start: a wave runs once every earlier wave it needs is done.

    A dependency on an unknown work order is ignored; a cycle puts the rest in one last wave.
    """
    known = set(deps)
    level: dict[str, int] = {}
    remaining = {wo: [d for d in ds if d in known] for wo, ds in deps.items()}
    while remaining:
        ready = [wo for wo, ds in remaining.items() if all(d in level for d in ds)]
        if not ready:
            last = max(level.values(), default=-1) + 1
            for wo in remaining:
                level[wo] = last
            break
        for wo in ready:
            level[wo] = 1 + max((level[d] for d in remaining[wo]), default=-1)
            remaining.pop(wo)
    waves: list[list[str]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
    for wo in deps:
        waves[level[wo]].append(wo)
    return waves


def delivery_plan(markdown: str) -> dict[str, Any]:
    """Waves, the critical path length and oversized work orders, with plain-language warnings.

    Delivery time is set by the number of waves (the longest dependency chain), not the number
    of work orders, so a long chain of single work orders is the slow shape. A very large work
    order is the other failure: one coding run that is hard to finish and to review.
    """
    deps = parse_work_order_dependencies(markdown)
    waves = delivery_waves(deps)
    sections = _sections(markdown)
    large = []
    for wo, section in sections.items():
        files = _section_bullets(section, {"files", "touches"})
        requirements = _section_bullets(section, {"requirements"})
        if files > LARGE_WORK_ORDER_FILES or requirements > LARGE_WORK_ORDER_REQUIREMENTS:
            large.append({"id": wo, "files": files, "requirements": requirements})
    warnings: list[str] = []
    if waves:
        shape = " → ".join(", ".join(wave) for wave in waves)
        count = f"{len(deps)} work order{'s' if len(deps) != 1 else ''}"
        warnings.append(f"Delivery plan: {count} in {len(waves)} wave{'s' if len(waves) != 1 else ''} ({shape}).")
    single = [idx for idx, wave in enumerate(waves) if len(wave) == 1 and idx > 0]
    if len(waves) > 3 and len(single) >= 2:
        warnings.append(
            f"{len(single)} of the {len(waves)} waves run a single work order, so they run one after another. "
            "Check whether those dependencies are real, or merge tightly coupled work orders on the chain."
        )
    for item in large:
        warnings.append(
            f"{item['id']} looks large ({item['files']} files, {item['requirements']} requirements); "
            "consider splitting it along a seam that lets both parts run in parallel."
        )
    return {"work_orders": len(deps), "waves": waves, "large": large, "warnings": warnings}
