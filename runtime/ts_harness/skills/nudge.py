"""Nudge copy and revise-intent parse for the learning loop."""

from __future__ import annotations

import re

from ts_harness.skills.learn import LEARN_TOKEN_RE, strip_leading_mentions


NUDGE_TEXT = (
    "This looks reusable. `/learn` a new skill, or ask to revise an "
    "existing one."
)

REVISE_SYSTEM_INSTRUCTION = (
    "You are authoring a revision draft of an existing live skill. Research "
    "the session and source with existing tools. Then call propose_skill with "
    "complete skill markdown AND revision_of set to the live skill id. The "
    "markdown id must equal revision_of, category must match the live skill, "
    "and version must be bumped above the live version. Never write "
    "skills/<category>/ yourself. Never claim the skill is live. After "
    "propose_skill succeeds, tell the user the draft id and that Inbox must "
    "approve at /#/inbox/skill_draft/{id}."
)

_ID_TOKEN_RE = re.compile(r"[a-z][a-z0-9-]{1,62}")
_TOKEN_RE = re.compile(r"don't|[a-z][a-z0-9-]*", re.IGNORECASE)
_REVISE_BLOCKERS = frozenset(
    {
        "not",
        "don't",
        "dont",
        "never",
        "no",
        "should",
        "could",
        "might",
        "would",
        "may",
    }
)
_REVISE_HEAD_STOPWORDS = frozenset(
    {
        "please",
        "can",
        "you",
        "we",
        "i",
        "to",
        "the",
        "a",
        "an",
    }
)
_REVISE_CLAUSE_RE = re.compile(
    r"\b(?:then|by\s+the\s+way)\b|[.;?,]",
    re.IGNORECASE,
)
_REVISE_LEAD_IN_RE = re.compile(
    r"^\s*(?:please|(?:could|would|can)\s+you|let(?:'s|s|\s+s)|"
    r"(?:hey|hi|ok|okay|yes|just)(?:\s+(?:claw|harness))?)\s+",
    re.IGNORECASE,
)


def should_nudge(
    *, status: str, tool_calls: list[dict], already_sent: bool
) -> bool:
    if already_sent or status != "succeeded":
        return False
    records = list(tool_calls or [])
    allowed_records = [
        record
        for record in records
        if str(record.get("decision") or "allow") == "allow"
    ]
    if len(allowed_records) < 3:
        return False
    if any(
        str(record.get("tool") or "") == "propose_skill"
        for record in records
    ):
        return False
    return True


def apply_nudge_to_summary(summary: str) -> str:
    text = str(summary or "")
    if not text.strip():
        return ""
    if NUDGE_TEXT in text:
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + NUDGE_TEXT


def parse_revise_intent(text: str, live_ids: set[str]) -> str | None:
    stripped = strip_leading_mentions(text).strip()
    if not stripped:
        return None
    head, *_rest = stripped.split(None, 1)
    if LEARN_TOKEN_RE.match(head):
        return None
    clause = next(
        (
            candidate.strip()
            for candidate in _REVISE_CLAUSE_RE.split(stripped)
            if re.search(r"\brevise\b", candidate, re.IGNORECASE)
        ),
        "",
    )
    if not clause:
        return None
    while True:
        updated = _REVISE_LEAD_IN_RE.sub("", clause, count=1)
        if updated == clause:
            break
        clause = updated
    revise_match = re.search(r"\brevise\b", clause, re.IGNORECASE)
    if revise_match is None:
        return None
    before = clause[: revise_match.start()]
    prior_tokens = _TOKEN_RE.findall(before.lower())
    if any(token not in _REVISE_HEAD_STOPWORDS for token in prior_tokens):
        return None
    # These words make "revise" negated or modal rather than an imperative.
    if any(token in _REVISE_BLOCKERS for token in prior_tokens[-4:]):
        return None
    after_tokens = _ID_TOKEN_RE.findall(clause[revise_match.end():].lower())
    hits = {token for token in after_tokens if token in live_ids}
    if len(hits) != 1:
        return None
    return next(iter(hits))
