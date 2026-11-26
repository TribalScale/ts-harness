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



# API contract rules: deferred work gets its own work order, and the API contract comes first.
API_ROLES = frozenset({"api", "backend", "server", "service"})
WEB_ROLES = frozenset({"web", "frontend", "ui", "spa", "app"})
_DELIVERABLE = (
    r"(?:backend|api(?!\s+(?:response|request|call)s?\b)|endpoints?|services?|openapi(?:\.json)?|spec|schema"
    r"|WO-\d+|work order|migration|integration|real data)"
)
_ARRIVES = (
    r"(?:exists?|lands?|ships?|is (?:built|available|merged|implemented|copied(?: in)?|added|in place|live|deployed"
    r"|published|delivered|ready)|are (?:built|available|merged|implemented|added|in place|delivered|ready))"
)
_DEFERRALS = (
    re.compile(rf"\b(?:until|once)\b[^.\n]{{0,60}}?\b{_DELIVERABLE}\b[^.\n]{{0,40}}?\b{_ARRIVES}\b", re.IGNORECASE),
    re.compile(r"\bfor now\b", re.IGNORECASE),
    re.compile(
        r"\b(?:will be|to be|can be|is|are) (?:added|done|handled|implemented|replaced|wired(?: up)?|fixed|built"
        r"|generated|completed|addressed|regenerated) later\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bin a (?:later|future|follow-up) (?:work order|phase|ticket|iteration|pr)\b", re.IGNORECASE),
    re.compile(r"\bTODO\b"),
)
_FOLLOW_UP = re.compile(r"^\s*(?:[-*]\s+)?\|?\s*\*{0,2}Follow[- ]ups?\*{0,2}\s*(?::|\|)\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_ENDPOINT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/[A-Za-z0-9_\-/{}:.]*)")
_HAND_WRITTEN = re.compile(
    r"\bhand[- ]?(?:written|write|writing|code|coded|craft(?:ed)?)\b[^.\n]{0,40}\b(?:types?|interfaces?|models?|dtos?)\b"
    r"|\b(?:types?|interfaces?|dtos?)\b[^.\n]{0,40}\bby hand\b"
    r"|\b(?:define|declare|write)\b[^.\n]{0,30}\b(?:request|response)\s+(?:types?|interfaces?)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(?:never|not|no|don't|do not|instead of|rather than|forbid(?:den|s)?)\b", re.IGNORECASE)
_GENERATES_TYPES = re.compile(r"api:generate|openapi-typescript|generat\w*[^.\n]{0,40}\b(?:openapi|spec|schema)", re.IGNORECASE)


def _sentences(section: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+|\n", section) if part.strip()]


def _endpoints(section: str) -> set[str]:
    found = set()
    for method, path in _ENDPOINT.findall(section):
        normal = re.sub(r"\{[^}]*\}|:[A-Za-z_]\w*", "{}", path.rstrip("/.`") or "/").lower()
        found.add(f"{method.upper()} {normal}")
    return found


_TEST_HEADING = re.compile(r"^#{2,4}\s+.*\b(?:acceptance|tests?|scenarios?|verification)\b.*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,4}\s+\S")
_GHERKIN_LINE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:\*\*)?(?:Given|When|Then|And|But)\b", re.IGNORECASE)
_NO_ENDPOINT_CHANGES = re.compile(
    r"\bendpoints?\s*:\s*none\b|\bno\s+(?:new\s+or\s+changed\s+|api\s+)?endpoints?\s+(?:are\s+)?(?:added|changed)\b"
    r"|\bdoes(?:n't| not)\s+(?:add|change)\s+(?:any\s+)?endpoints?\b",
    re.IGNORECASE,
)


def _changed_endpoints(section: str) -> set[str]:
    """Endpoints an API work order adds or changes: those named outside its tests.

    A work order's acceptance tests call endpoints that already exist (a template adoption proves
    smoke mode with `GET /me`); those calls don't change the contract. So endpoints in a test
    section (a heading with "acceptance", "test" or "scenario") or on a Given/When/Then line don't
    count, and a work order that states it changes no endpoint ("Endpoints: none") has none.
    """
    if _NO_ENDPOINT_CHANGES.search(section or ""):
        return set()
    kept: list[str] = []
    in_tests = False
    for line in (section or "").splitlines():
        if _ANY_HEADING.match(line):
            in_tests = bool(_TEST_HEADING.match(line))
            continue
        if in_tests or _GHERKIN_LINE.match(line):
            continue
        kept.append(line)
    return _endpoints("\n".join(kept))


def _reachable(start: str, deps: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(deps.get(start, []))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(deps.get(node, []))
    return seen


def deferred_work_problems(markdown: str) -> list[str]:
    """Work orders that defer part of their job ("until the API exists", "for now") without a follow-up.

    Deferred work that lives only in a sentence is forgotten: nothing tracks it, so the
    stand-in ships. The work order names its follow-up (``Follow-up: WO-009``) and that
    follow-up depends on the work order producing what is missing, so it runs once it can.
    """
    sections = _sections(markdown)
    deps = parse_work_order_dependencies(markdown)
    problems: list[str] = []
    for wo_id, section in sections.items():
        body = _FOLLOW_UP.sub("", section)
        own = set(deps.get(wo_id, []))
        # "once WO-003 is merged" in a work order that depends on WO-003 is a declared dependency.
        hit = next(
            (
                m.group(0)
                for pattern in _DEFERRALS
                for m in pattern.finditer(body)
                if not own & {ref.upper() for ref in _WO_ID.findall(m.group(0))}
            ),
            None,
        )
        if hit is None:
            continue
        follow_ups = [
            ref.upper()
            for line in _FOLLOW_UP.findall(section)
            for ref in _WO_ID.findall(line)
            if ref.upper() != wo_id
        ]
        if not follow_ups:
            problems.append(
                f'{wo_id} defers work ("{hit.strip()}") without a follow-up work order; add one that depends on the '
                f"work order producing what is missing and name it in a `Follow-up: WO-NNN` line"
            )
            continue
        for ref in follow_ups:
            if ref not in sections:
                problems.append(f"{wo_id} names follow-up {ref}, which is not in the plan")
            elif not deps.get(ref):
                problems.append(
                    f"{wo_id}'s follow-up {ref} depends on nothing; it must depend on the work order that produces "
                    "what it waits for"
                )
    return problems


def api_contract_problems(markdown: str, repositories: list[dict[str, Any]] | None) -> list[str]:
    """Where the plan would let a web app's API types drift from the backend's contract.

    An API work order that adds or changes endpoints updates the committed ``openapi.json``.
    A web work order that calls those endpoints depends on that work order and generates its
    types from the spec (``npm run api:pull && npm run api:generate``); it never writes them by
    hand. Roles come from ``repositories``; without them only the hand-written-types rule applies.
    """
    sections = _sections(markdown)
    deps = parse_work_order_dependencies(markdown)
    named = {k: v.lower() for k, v in parse_work_order_repositories(markdown).items()}
    roles = {str(r.get("full_name") or "").lower(): str(r.get("role") or "").lower() for r in repositories or []}
    if len(repositories or []) == 1 and roles:
        only = next(iter(roles))
        named = {wo_id: named.get(wo_id, only) for wo_id in sections}
    role_of = {wo_id: roles.get(named.get(wo_id, ""), "") for wo_id in sections}
    problems: list[str] = []
    providers: dict[str, set[str]] = {}
    for wo_id, section in sections.items():
        if role_of[wo_id] in API_ROLES:
            endpoints = _changed_endpoints(section)
            if endpoints:
                providers[wo_id] = endpoints
                if "openapi" not in section.lower():
                    problems.append(
                        f"{wo_id} adds or changes endpoints but does not update the committed openapi.json; "
                        "make the updated spec part of its acceptance"
                    )
    for wo_id, section in sections.items():
        if role_of[wo_id] in API_ROLES:
            continue
        for sentence in _sentences(section):
            if _HAND_WRITTEN.search(sentence) and not _NEGATION.search(sentence):
                problems.append(
                    f'{wo_id} hand-writes API types ("{sentence.strip()[:120]}"); generate them from the backend\'s '
                    "openapi.json with `npm run api:pull && npm run api:generate`"
                )
                break
        if role_of[wo_id] not in WEB_ROLES:
            continue
        before = _reachable(wo_id, deps)
        consumed = _endpoints(section)
        relied = [p for p, endpoints in providers.items() if p != wo_id and (p in before or consumed & endpoints)]
        for provider in sorted(p for p in relied if p not in before):
            calls = ", ".join(sorted(consumed & providers[provider]))
            problems.append(f"{wo_id} calls {calls}, which {provider} adds; it must depend on {provider}")
        if relied and not _GENERATES_TYPES.search(section):
            problems.append(
                f"{wo_id} uses endpoints from {', '.join(sorted(relied))} but does not generate its API types from the "
                "updated spec; require `npm run api:pull && npm run api:generate`"
            )
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
