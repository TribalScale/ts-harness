"""Improvement proposals: what they are, how they move, and what the loop may act on.

A proposal is one concrete change the weekly failure-pattern digest suggested
(:mod:`ts_harness.improvement.digest`), stored in ``improvement_proposals`` (migration 056) behind
the memory API. Types:

* ``knowledge_note``: a proposed note, created through the existing proposed-notes path; a person
  approves it in the Inbox as a note, and the proposal follows the note's status;
* ``skill_edit``: a unified diff to a file under ``skills/``;
* ``setting_change``: a program default or runtime setting, old value -> new value;
* ``fragment_change``: a diff to ``config/fragments/*`` or ``config/workflows/*``;
* ``needs_design``: an idea whose target is on the off-limits list. It keeps the idea and the
  evidence, never a diff, and is never acted on: a person has to design the change.

Lifecycle (``TRANSITIONS``)::

    proposed -> evaluating -> canary -> awaiting_approval -> promoted
                    |            |              |---------> rolled_back / rejected
                    |            '-> rolled_back
                    '-> proposed (inconclusive, or the target's canary slot is taken) / rejected
    proposed -> promoted / rejected  (a person decides one that has no offline check)

Only a proposal that passed an offline replay check may start a canary, and only the loop starts
one. Promotion always needs a person.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ts_harness.improvement import patches
from ts_harness.improvement.off_limits import is_off_limits

logger = logging.getLogger(__name__)

TYPES = ("knowledge_note", "skill_edit", "setting_change", "fragment_change", "needs_design")
ACTIONABLE_TYPES = ("knowledge_note", "skill_edit", "setting_change", "fragment_change")
STATUSES = ("proposed", "evaluating", "canary", "awaiting_approval", "promoted", "rolled_back", "rejected")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("evaluating", "promoted", "rejected"),
    "evaluating": ("proposed", "canary", "rejected"),
    "canary": ("awaiting_approval", "rolled_back"),
    "awaiting_approval": ("promoted", "rolled_back", "rejected"),
    "promoted": (),
    "rolled_back": (),
    "rejected": (),
}
TERMINAL = frozenset({"promoted", "rolled_back", "rejected"})
OPEN = frozenset(STATUSES) - TERMINAL
ACTOR = "harness-improver"

# The pre-push reviewer is the one target with an offline replay check (see replay.py).
PRE_PUSH_TARGET = "pre_push_review"
GATE_SKILL_PATH = "skills/review/governed-code-review/SKILL.md"
SKILL_PREFIX = "skills/"
FRAGMENT_PREFIXES = ("config/fragments/", "config/workflows/")

# Offline check outcomes recorded on ``evaluation.status``.
EVAL_PASSED = "passed"
EVAL_FAILED = "failed"
EVAL_INCONCLUSIVE = "inconclusive"
EVAL_UNAVAILABLE = "unavailable"
EVAL_NOT_NEEDED = "not_needed"
NO_OFFLINE_CHECK = "no offline check available"


def _text(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def pre_push_sections() -> tuple[str, ...]:
    """The skill sections the pre-push reviewer reads (its rubric)."""
    from ts_harness.implementation.pre_push_review import GATE_SECTIONS

    return tuple(GATE_SECTIONS)


def editable_pre_push_sections() -> list[str]:
    """Rubric sections a proposal may change: the reviewer's sections that are not off limits."""
    return [s for s in pre_push_sections() if not is_off_limits(path=GATE_SKILL_PATH, section=s)]


class Rejected(ValueError):
    """The model's proposal cannot be used at all (malformed, or a diff that does not apply)."""


def _off_limits_in_diff(changed: dict[str, tuple[str, str]]) -> tuple[dict[str, Any] | None, dict[str, list[str]]]:
    """The first off-limits entry the changed files hit (per ``##`` section for Markdown), and the
    changed sections per Markdown file."""
    touched: dict[str, list[str]] = {}
    for path, (before, after) in changed.items():
        if path.endswith(".md"):
            sections = patches.changed_sections(before, after)
            touched[path] = sections
            for section in sections:
                hit = is_off_limits(path=path, section=section or "(preamble)")
                if hit:
                    return {**hit, "section": section}, touched
        else:
            hit = is_off_limits(path=path)
            if hit:
                return hit, touched
    return None, touched


def screen(draft: dict[str, Any], *, reader: Any = None) -> dict[str, Any]:
    """Turn a model's draft into a storable proposal, or raise :class:`Rejected`.

    Returns ``{"type", "target", "change", "offline_check"}``. A draft whose target is off limits
    comes back as ``needs_design`` (no diff, the reason recorded). ``offline_check`` is
    ``PRE_PUSH_TARGET`` when the replay can evaluate it, else ``""``.
    """
    kind = _text(draft.get("type"), 40)
    if kind not in ACTIONABLE_TYPES:
        raise Rejected(f"unknown proposal type {kind!r}")
    idea = _text(draft.get("summary"), 2000)

    def needs_design(target: str, hit: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "needs_design",
            "target": target,
            "change": {"original_type": kind, "idea": idea, "off_limits": {k: hit.get(k) for k in ("kind", "match", "why", "section") if hit.get(k)}},
            "offline_check": "",
        }

    if kind == "knowledge_note":
        note = draft.get("note") if isinstance(draft.get("note"), dict) else {}
        title, body = _text(note.get("title"), 100), _text(note.get("body"), 280)
        if not title or not body:
            raise Rejected("a knowledge note needs a title and a body")
        clean = {
            "scope": "project" if note.get("scope") == "project" else "org",
            "kind": "avoid" if note.get("kind") == "avoid" else "howto",
            "title": title,
            "body": body,
            "why": _text(note.get("why"), 400),
            "applies_to": [str(item)[:120] for item in (note.get("applies_to") or []) if str(item).strip()][:8],
        }
        return {"type": kind, "target": f"note:{clean['scope']}", "change": {"note": clean}, "offline_check": ""}

    if kind == "setting_change":
        setting = draft.get("setting") if isinstance(draft.get("setting"), dict) else {}
        name = _text(setting.get("name"), 200)
        if not name or any(ch.isspace() for ch in name):
            raise Rejected("a setting change needs the setting's name")
        hit = is_off_limits(setting=name) or is_off_limits(constant=name)
        if hit:
            return needs_design(f"setting:{name}", hit)
        change = {"setting": {"name": name, "old": setting.get("old"), "new": setting.get("new")}}
        if json.dumps(change["setting"]["old"], default=str) == json.dumps(change["setting"]["new"], default=str):
            raise Rejected("the new value is the same as the old one")
        return {"type": kind, "target": f"setting:{name}", "change": change, "offline_check": ""}

    diff = str(draft.get("diff") or "").strip("\n") + "\n"
    try:
        files = patches.parse(diff)
    except patches.PatchError as exc:
        raise Rejected(str(exc)) from exc
    paths = sorted({item.path for item in files})
    allowed = (SKILL_PREFIX,) if kind == "skill_edit" else FRAGMENT_PREFIXES
    for path in paths:
        hit = is_off_limits(path=path) if not path.endswith(".md") else None
        if hit:
            return needs_design(path, hit)
        if not path.startswith(allowed):
            raise Rejected(f"a {kind} may only change files under {', '.join(allowed)} (not {path})")
    try:
        changed = patches.apply(diff, reader=reader)
    except patches.PatchError as exc:
        raise Rejected(str(exc)) from exc
    hit, sections = _off_limits_in_diff(changed)
    if hit:
        return needs_design(", ".join(paths), hit)
    for path, (_, after) in changed.items():
        if path.endswith(".json"):
            try:
                json.loads(after)
            except ValueError as exc:
                raise Rejected(f"{path} is not valid JSON after the change: {exc}") from exc
    offline = ""
    target = ", ".join(paths)[:300]
    if kind == "skill_edit" and paths == [GATE_SKILL_PATH]:
        touched = [s for s in sections.get(GATE_SKILL_PATH, []) if s]
        if touched and set(touched) <= set(editable_pre_push_sections()):
            offline, target = PRE_PUSH_TARGET, PRE_PUSH_TARGET
    change = {
        "diff": diff,
        "files": paths,
        "sections": sections,
        "base": {path: patches.sha(before) for path, (before, _) in changed.items()},
    }
    return {"type": kind, "target": target, "change": change, "offline_check": offline}


# Store (memory API) helpers. Every write names the status it expects, so two writers cannot both
# move a proposal.


def _memory(memory: Any) -> Any:
    if memory is not None:
        return memory
    from ts_harness.memory.client import get_memory_client

    return get_memory_client()


def list_open(memory: Any = None, *, tenant_id: str = "default") -> list[dict[str, Any]]:
    return _memory(memory).list_improvement_proposals(tenant_id=tenant_id, status=",".join(sorted(OPEN)), limit=200)


def move(memory: Any, proposal: dict[str, Any], status: str, *, actor: str = ACTOR, reason: str = "",
         tenant_id: str = "default", **fields: Any) -> dict[str, Any]:
    """Move ``proposal`` to ``status`` (checked here and by the memory API) with any of
    ``evaluation``, ``canary``, ``promotion``, ``change``, ``add_cost_usd``."""
    current = str(proposal.get("status") or "")
    if status != current and status not in TRANSITIONS.get(current, ()):
        raise ValueError(f"a {current} proposal cannot become {status}")
    return _memory(memory).update_improvement_proposal(
        proposal["id"], tenant_id=tenant_id, expected_status=current, status=status, actor=actor, reason=reason,
        **fields,
    )


def annotate(memory: Any, proposal: dict[str, Any], *, tenant_id: str = "default", **fields: Any) -> dict[str, Any]:
    """Update fields without moving the status (still conditional on it)."""
    return _memory(memory).update_improvement_proposal(
        proposal["id"], tenant_id=tenant_id, expected_status=str(proposal.get("status") or ""), **fields
    )


def variant_name(proposal: dict[str, Any] | int | str) -> str:
    """The ticket variant a proposal's canary records: ``proposal-<id>``."""
    number = proposal.get("id") if isinstance(proposal, dict) else proposal
    return f"proposal-{int(str(number).removeprefix('proposal-'))}"


def needs_person(proposal: dict[str, Any]) -> bool:
    """Shown in the Inbox: waiting for a person's decision (or design)."""
    status = str(proposal.get("status") or "")
    if status == "awaiting_approval":
        return True
    if status != "proposed":
        return False
    if proposal.get("type") == "needs_design":
        return True
    if proposal.get("type") == "knowledge_note":
        return False  # the note itself is in the Inbox
    evaluation = proposal.get("evaluation") if isinstance(proposal.get("evaluation"), dict) else {}
    return evaluation.get("status") == EVAL_UNAVAILABLE
