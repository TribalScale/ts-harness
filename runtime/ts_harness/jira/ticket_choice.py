"""What a person picked when replying to the ticket list (``pick_ticket``)."""

from __future__ import annotations

import re
from typing import Any

_ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9_]{1,10}-\d+)\b")
_ALL = re.compile(r"\b(?:all|every(?:thing)?|the rest)\b\s*(?:of\s+|in\s+)?([A-Za-z][A-Za-z0-9_]{1,10})?", re.IGNORECASE)
_YES = re.compile(r"^\s*(?:y|yes|ok|okay|go|ready|sure|confirm(?:ed)?)\b", re.IGNORECASE)


def ticket_choice_from_reply(reply: str, listing: dict[str, Any]) -> dict[str, Any]:
    """``jira_issue_key`` for a named ticket; ``jira_issue_keys`` for ``all <PROJECT>``.

    ``listing`` is ``prepare_ticket_queue``'s output (``tickets``, ``queues``, ``queue``). A
    plain "yes" takes the only listed ticket, or confirms a frozen queue. Returns nothing it
    cannot read, so the gate's reply text still says what the person wrote.
    """
    text = str(reply or "")
    keys = [key.upper() for key in _ISSUE_KEY.findall(text.upper())]
    if keys:
        key = keys[0]
        return {"jira_issue_key": key, "jira_project_key": key.split("-", 1)[0]}
    queues = listing.get("queues") if isinstance(listing.get("queues"), dict) else {}
    wanted = _ALL.search(text)
    if wanted and queues:
        project = (wanted.group(1) or "").upper()
        if project not in queues and len(queues) == 1:
            project = next(iter(queues))  # "all", "all of them": the only project listed
        queue = queues.get(project) if project else None
        if isinstance(queue, dict) and queue.get("jira_issue_keys"):
            ordered = [str(k) for k in queue["jira_issue_keys"]]
            return {
                "jira_project_key": project,
                "jira_issue_key": ordered[0],
                "jira_issue_keys": ordered,
                "queue_exclusions": list(queue.get("queue_exclusions") or []),
            }
    if _YES.match(text):
        tickets = [str(k) for k in listing.get("tickets") or [] if k]
        if len(tickets) == 1:
            return {"jira_issue_key": tickets[0], "jira_project_key": tickets[0].split("-", 1)[0]}
    return {}


def names_a_pick(reply: str) -> bool:
    """True when a reply to the ticket list picks something: a ticket key, or ``all ...``."""
    text = str(reply or "")
    return bool(_ISSUE_KEY.search(text.upper()) or _ALL.search(text))
