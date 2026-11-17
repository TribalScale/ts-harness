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


def named_ticket_key(summary: str) -> str:
    """The single issue key in ``implement PROJ-184``. Empty when the text names none."""
    match = _NAMED_KEY.search(summary or "")
    return match.group(1).upper() if match else ""


def wants_ticket_queue(summary: str) -> bool:
    """True for ``implement all`` and ``implement the rest``. A named key is one ticket."""
    if named_ticket_key(summary):
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
        outside = sorted({blocker for blocker in blockers if blocker not in candidates})
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

    ready = [key for key, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        key = heapq.heappop(ready)
        ordered.append(key)
        for child in dependents[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)

    for key in sorted(nodes - set(ordered)):
        excluded.append({"key": key, "reason": "cycle"})

    if len(ordered) > QUEUE_CAP:
        for key in ordered[QUEUE_CAP:]:
            excluded.append({"key": key, "reason": _CAP_REASON})
        ordered = ordered[:QUEUE_CAP]

    excluded.sort(key=lambda item: (item["key"], item["reason"]))
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
