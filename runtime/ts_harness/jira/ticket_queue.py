"""Order assigned Jira tickets into one confirmed serial queue."""

from __future__ import annotations

import heapq
import re
from typing import Any

from ts_harness.jira.spec_hydrate import open_blockers
from ts_harness.workflows.program_materialize import DEFAULT_LIST_MAX_ITERATIONS

QUEUE_CAP = DEFAULT_LIST_MAX_ITERATIONS
_CAP_REASON = f"queue cap {QUEUE_CAP}"

_NAMED_KEY = re.compile(r"implement\s+([a-z][a-z0-9]+-\d+)\b", re.IGNORECASE)
_IMPLEMENT_ALL = re.compile(r"\bimplement all\b", re.IGNORECASE)
_IMPLEMENT_REST = re.compile(r"\bimplement the rest\b", re.IGNORECASE)


_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9]+)-(\d+)\b")
# More numbers of the same project after a key: "PROJ-34, 35, 36 and 37".
_MORE_NUMBERS = re.compile(r"\s*(?:,|\band\b|&)\s*(\d+)\b(?!-)")
_NEGATION = re.compile(
    r"\b(?:except(?:\s+for)?|excluding|exclude|but\s+not|not|skip(?:ping)?|without|other\s+than|apart\s+from|"
    r"don'?t|do\s+not|never|leave\s+out|minus)\b",
    re.IGNORECASE,
)
# Where a negation's reach ends: a new sentence or a new "and ..." part of the request.
_CLAUSE_BREAK = re.compile(r"[.;:\n()]|,\s*(?:and|then|plus|also)\b|\band\s+(?:the|also|then)\b", re.IGNORECASE)
_EPIC_WORD = re.compile(r"\bepics?\b", re.IGNORECASE)


def _keys_in(clause: str) -> list[tuple[int, str]]:
    """``(position, KEY)`` for each issue key in ``clause``, with "PROJ-34, 35" read as PROJ-34 and PROJ-35."""
    found: list[tuple[int, str]] = []
    for match in _KEY.finditer(clause):
        project = match.group(1).upper()
        found.append((match.start(), f"{project}-{match.group(2)}"))
        end = match.end()
        while True:
            more = _MORE_NUMBERS.match(clause, end)
            if not more:
                break
            found.append((more.start(1), f"{project}-{more.group(1)}"))
            end = more.end()
    return found


def ticket_request(text: str) -> dict[str, Any]:
    """The tickets a message asks for: ``{included, excluded, epic}``.

    Keys after a negation in the same part of the message ("except PROJ-45", "do not implement
    the epic PROJ-33") are excluded; every other key is included, in the order written. ``epic`` is
    true when the message talks about an epic, so its keys may need expanding into their children.
    """
    included: list[str] = []
    excluded: list[str] = []
    start = 0
    text = text or ""
    for brk in [*_CLAUSE_BREAK.finditer(text), None]:
        end = brk.start() if brk else len(text)
        clause = text[start:end]
        negation = _NEGATION.search(clause)
        for position, key in _keys_in(clause):
            target = excluded if negation and position > negation.start() else included
            if key not in target:
                target.append(key)
        start = brk.end() if brk else len(text)
    included = [key for key in included if key not in excluded]
    return {"included": included, "excluded": excluded, "epic": bool(_EPIC_WORD.search(text))}


def wants_explicit_queue(text: str) -> bool:
    """The message names several tickets, or an epic: a queue of exactly those, not one ticket."""
    request = ticket_request(text)
    return len(request["included"]) > 1 or (bool(request["included"]) and request["epic"])


def named_ticket_key(summary: str) -> str:
    """The single issue key in ``implement PROJ-184``. Empty when the text names none.

    A key the message excludes ("do not implement PROJ-9") is never the one, and a message that
    names several tickets or an epic is a queue, not one ticket.
    """
    if wants_explicit_queue(summary):
        return ""
    match = _NAMED_KEY.search(summary or "")
    if not match:
        return ""
    key = match.group(1).upper()
    return "" if key in ticket_request(summary)["excluded"] else key


def wants_ticket_queue(summary: str) -> bool:
    """True for ``implement all`` and ``implement the rest``. A named key is one ticket."""
    if named_ticket_key(summary) or wants_explicit_queue(summary):
        return False
    text = summary or ""
    return bool(_IMPLEMENT_ALL.search(text) or _IMPLEMENT_REST.search(text))


def _issue_key(issue: dict[str, Any]) -> str:
    return str(issue.get("key") or "").strip()


def _issue_links(issue: dict[str, Any]) -> list[dict[str, Any]]:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    links = fields.get("issuelinks")
    if links is None:
        links = issue.get("issuelinks")
    if not isinstance(links, list):
        return []
    return [link for link in links if isinstance(link, dict)]


def issue_sort_key(key: str) -> tuple[str, int, str]:
    """``PROJ-7`` before ``PROJ-22``: by project, then issue number, not as text."""
    match = re.match(r"^([A-Za-z][A-Za-z0-9]*)-(\d+)$", str(key or "").strip())
    if not match:
        return (str(key or ""), 0, str(key or ""))
    return (match.group(1).upper(), int(match.group(2)), str(key))


def order_ticket_queue(issues: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Order candidates so open blockers come first.

    A ticket stays in the queue only when every open blocker is also a candidate.
    An outside blocker, a cycle, or the 51st ready key is returned in ``excluded``.
    """
    candidates: dict[str, dict[str, Any]] = {}
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        key = _issue_key(issue)
        if key and key not in candidates:
            candidates[key] = issue

    excluded: list[dict[str, str]] = []
    nodes: set[str] = set()
    blockers_of: dict[str, list[str]] = {}
    for key, issue in candidates.items():
        blockers = open_blockers(_issue_links(issue))
        outside = sorted({blocker for blocker in blockers if blocker not in candidates}, key=issue_sort_key)
        if outside:
            excluded.append({"key": key, "reason": "blocked by " + ", ".join(outside)})
            continue
        nodes.add(key)
        blockers_of[key] = [blocker for blocker in blockers if blocker in candidates]

    dependents: dict[str, list[str]] = {key: [] for key in nodes}
    indegree = {key: 0 for key in nodes}
    for key, blockers in blockers_of.items():
        seen: set[str] = set()
        for blocker in blockers:
            if blocker not in nodes or blocker in seen or blocker == key:
                continue
            seen.add(blocker)
            dependents[blocker].append(key)
            indegree[key] += 1

    # Among ready tickets, the lowest issue number goes first (PROJ-7 before PROJ-22).
    ready = [(issue_sort_key(key), key) for key, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        _, key = heapq.heappop(ready)
        ordered.append(key)
        for child in dependents[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (issue_sort_key(child), child))

    for key in sorted(nodes - set(ordered), key=issue_sort_key):
        excluded.append({"key": key, "reason": "cycle"})

    if len(ordered) > QUEUE_CAP:
        for key in ordered[QUEUE_CAP:]:
            excluded.append({"key": key, "reason": _CAP_REASON})
        ordered = ordered[:QUEUE_CAP]

    excluded.sort(key=lambda item: (issue_sort_key(item["key"]), item["reason"]))
    return {"ordered": ordered, "excluded": excluded}


def remaining_queue_keys(artifacts: dict[str, Any] | None) -> list[str]:
    """Keys in ``jira_issue_keys`` that are not in ``completed_jira_issue_keys``."""
    bag = artifacts or {}
    keys = bag.get("jira_issue_keys")
    if not isinstance(keys, list):
        return []
    completed = {str(item) for item in list(bag.get("completed_jira_issue_keys") or [])}
    return [key for key in keys if isinstance(key, str) and key and key not in completed]


def queue_is_active(artifacts: dict[str, Any] | None) -> bool:
    return bool(remaining_queue_keys(artifacts))
