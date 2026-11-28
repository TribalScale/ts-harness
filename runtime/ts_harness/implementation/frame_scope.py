"""Which part of a shared design frame each work order or ticket owns.

Several work orders often build parts of the same screen in parallel: one the schedule
blocks, one the grid rows and their tones, one the week header and toolbar. Each names the
same frame (``- Design: D1``) and each says "Then the page matches D1". Judged against the
whole frame, none of them can pass until all of them merge, and QA blocks each ticket on
the parts another ticket owns.

So a work order that shares a frame with a sibling (another work order of the same plan,
or another ticket in the same queue or program) carries a ``## Frame scope`` section::

    ## Frame scope

    ### D1
    - Owns:
      - schedule blocks on the week grid (block cards, progress bars, late badges)
    - Owned by other tickets:
      - PROJ-12: grid rows and their tones
      - PROJ-13: week header and toolbar

The coding agent builds the owned part; QA and code review judge only the owned part, and
differences in the rest are notes, not defects. The whole-frame match is left to the
nightly design conformance check (``ts_harness.workflows.design_conformance``), which
reports gaps between tickets.

The planner writes the section for planned work orders (and its contract rejects a shared
frame without one, or two work orders claiming the same region). ``prepare_ticket_delivery``
derives it for Jira tickets from the siblings that name the same frames (``## Shared
frames``), from each ticket's In Scope list, Files list or summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FRAME_SCOPE_HEADING = "## Frame scope"
SHARED_FRAMES_HEADING = "## Shared frames"
AMENDMENT_TITLE = "Scope amendment"

_WO_HEADER = re.compile(r"^#{1,3}\s+(WO-\d+)\b.*$", re.IGNORECASE | re.MULTILINE)
_SCOPE = re.compile(r"^#{2,3}\s+Frame scope\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_SECTION = re.compile(r"^#{1,2}\s+\S", re.MULTILINE)
_ENTRY = re.compile(r"^#{3,4}\s+(D\d{1,3})\b.*$", re.MULTILINE)
_OWNS = re.compile(r"^\s*[-*]\s+\*{0,2}Owns\*{0,2}\s*:\s*(.*)$", re.IGNORECASE)
_OTHERS = re.compile(
    r"^\s*[-*]\s+\*{0,2}(?:Owned by other (?:tickets|work orders)|Owned elsewhere|Not owned|Others)\*{0,2}\s*:\s*(.*)$",
    re.IGNORECASE,
)
_SUB = re.compile(r"^\s{2,}[-*]\s+(.+)$")
_OWNER_PREFIX = re.compile(r"^\s*\*{0,2}([A-Z][A-Z0-9]+-\d+)\*{0,2}\s*[:—–-]\s*(.+)$")
_OWNER_SUFFIX = re.compile(r"^(.+?)\s*\(\s*([A-Z][A-Z0-9]+-\d+)\s*\)\s*$")
TICKET_REF = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_AMENDMENT = re.compile(r"^##\s+Scope amendment\b.*$", re.IGNORECASE | re.MULTILINE)
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")
_STOP = frozenset(
    "a an the and or of its their with on in for to by at as is are be that this from into each all any "
    "part parts area areas section sections screen page".split()
)
MAX_ITEMS = 6
MAX_ITEM_CHARS = 140


@dataclass
class FrameScope:
    """One frame's scope in one work order: what it owns and who owns the rest."""

    id: str
    owns: list[str] = field(default_factory=list)
    others: list[dict[str, str]] = field(default_factory=list)  # {"owner", "region"}

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "owns": list(self.owns), "others": [dict(o) for o in self.others]}


def _section(body: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(body or "")
    if not match:
        return None
    rest = body[match.end() :]
    end = _NEXT_SECTION.search(rest)
    return rest[: end.start()] if end else rest


def _split_inline(value: str) -> list[str]:
    return [part.strip() for part in re.split(r";\s*", value or "") if part.strip()]


def _other(text: str) -> dict[str, str]:
    text = text.strip().strip("*").strip()
    match = _OWNER_PREFIX.match(text)
    if match:
        return {"owner": match.group(1), "region": match.group(2).strip()}
    match = _OWNER_SUFFIX.match(text)
    if match:
        return {"owner": match.group(2), "region": match.group(1).strip()}
    found = TICKET_REF.search(text)
    return {"owner": found.group(1) if found else "", "region": text}


def parse_frame_scope(body: str) -> dict[str, FrameScope]:
    """The ``## Frame scope`` entries of one work order, by frame id (empty when it has none)."""
    text = _section(body, _SCOPE)
    if text is None:
        return {}
    scopes: dict[str, FrameScope] = {}
    current: FrameScope | None = None
    mode = ""
    for line in text.splitlines():
        entry = _ENTRY.match(line)
        if entry:
            ref = f"D{int(entry.group(1)[1:])}"
            current = scopes.setdefault(ref, FrameScope(id=ref))
            mode = ""
            continue
        if current is None:
            continue
        owns = _OWNS.match(line)
        if owns:
            mode = "owns"
            current.owns.extend(_split_inline(owns.group(1)))
            continue
        others = _OTHERS.match(line)
        if others:
            mode = "others"
            current.others.extend(_other(part) for part in _split_inline(others.group(1)))
            continue
        sub = _SUB.match(line)
        if sub and mode == "owns":
            current.owns.append(sub.group(1).strip())
        elif sub and mode == "others":
            current.others.append(_other(sub.group(1)))
    return scopes


def render_frame_scope(scopes: list[FrameScope], *, others_label: str = "Owned by other tickets") -> str:
    """The ``## Frame scope`` section for ``scopes``."""
    lines = [FRAME_SCOPE_HEADING, ""]
    for scope in scopes:
        lines.append(f"### {scope.id}")
        lines.append("- Owns:")
        lines.extend(f"  - {item}" for item in scope.owns or ["(not stated)"])
        if scope.others:
            lines.append(f"- {others_label}:")
            lines.extend(
                f"  - {o['owner']}: {o['region']}" if o.get("owner") else f"  - {o['region']}" for o in scope.others
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _clean(text: str) -> str:
    text = re.sub(r"[`*_]+", "", text or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_ITEM_CHARS else text[: MAX_ITEM_CHARS - 1].rstrip() + "…"


def _bullets_under(body: str, heading: re.Pattern[str]) -> list[str]:
    match = heading.search(body or "")
    if not match:
        return []
    rest = body[match.end() :]
    end = re.search(r"^#{1,6}\s+\S|^\s*\*{0,2}[A-Z][A-Za-z ]{2,30}\*{0,2}\s*:\s*$", rest, re.MULTILINE)
    block = rest[: end.start()] if end else rest
    inline = match.groupdict().get("inline") if match.groupdict() else None
    items = [_clean(m.group(1)) for m in re.finditer(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$", block, re.MULTILINE)]
    if inline and inline.strip():
        items = [_clean(part) for part in re.split(r"[;,]\s*", inline) if part.strip()] + items
    return [item for item in items if item and not _FIELD_LINE.match(item)][:MAX_ITEMS]


_IN_SCOPE = re.compile(
    r"^(?:#{1,6}[ \t]+|[ \t]*\*{0,2})In[ -]scope\*{0,2}[ \t]*:?\*{0,2}[ \t]*(?P<inline>[^\n]*)$", re.IGNORECASE | re.MULTILINE
)
_FILES = re.compile(
    r"^(?:#{1,6}[ \t]+|[ \t]*[-*]?[ \t]*\*{0,2})Files(?: to (?:change|touch|create))?\*{0,2}[ \t]*:?\*{0,2}[ \t]*(?P<inline>[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_FIELD_LINE = re.compile(r"^(?:Design|Repo|Program|Work order|Route|State|Shows|Complexity|BDD)\s*:", re.IGNORECASE)
_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def scope_items(body: str, *, title: str = "") -> list[str]:
    """What a work order or ticket covers: its In Scope list, else its Files, else its title."""
    items = _bullets_under(body, _IN_SCOPE)
    if items:
        return items
    files = _bullets_under(body, _FILES)
    if files:
        return ["files: " + ", ".join(files)]
    heading = title or (_TITLE.search(body or "").group(1) if _TITLE.search(body or "") else "")
    heading = re.sub(r"^(?:[A-Z][A-Z0-9]+-\d+|WO-\d+)\s*[:·\-–—]?\s*", "", heading.strip())
    return [_clean(heading)] if heading.strip() else []


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", re.sub(r"\([^)]*\)", " ", (text or "").lower()))
    out = set()
    for word in words:
        if word in _STOP or len(word) < 3:
            continue
        out.add(word[:-1] if word.endswith("s") and len(word) > 3 else word)
    return out


def regions_overlap(a: str, b: str) -> bool:
    """Two region descriptions claim the same thing: the same words, or one inside the other."""
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return False
    return left <= right or right <= left


def amendment_sections(body: str) -> list[str]:
    """Every ``## Scope amendment (by <person>, <date>)`` section of a work order, oldest first."""
    found = []
    matches = list(_AMENDMENT.finditer(body or ""))
    for match in matches:
        rest = body[match.start() :]
        end = _NEXT_SECTION.search(rest, len(match.group(0)))
        found.append(rest[: end.start()].strip() if end else rest.strip())
    return found


# ── Planned work orders ──────────────────────────────────────────────────────


def _sections(markdown: str) -> dict[str, str]:
    text = (markdown or "").strip()
    matches = list(_WO_HEADER.finditer(text))
    return {
        match.group(1).upper(): text[match.start() : matches[idx + 1].start() if idx + 1 < len(matches) else len(text)]
        for idx, match in enumerate(matches)
    }


def shared_frames(entries: dict[str, str]) -> dict[str, list[str]]:
    """``{frame id: [refs]}`` for every frame that more than one of ``entries`` (ref -> body) names."""
    from ts_harness.implementation.frame_checklist import design_ids

    named: dict[str, list[str]] = {}
    for ref, body in entries.items():
        for frame in design_ids(body):
            named.setdefault(frame, []).append(ref)
    return {frame: refs for frame, refs in named.items() if len(refs) > 1}


def overlapping_claims(entries: dict[str, str]) -> list[dict[str, str]]:
    """Two work orders that both claim the same region of a shared frame."""
    shared = shared_frames(entries)
    scopes = {ref: parse_frame_scope(body) for ref, body in entries.items()}
    found: list[dict[str, str]] = []
    for frame, refs in sorted(shared.items(), key=lambda item: int(item[0][1:])):
        for i, left in enumerate(refs):
            for right in refs[i + 1 :]:
                a = scopes[left].get(frame)
                b = scopes[right].get(frame)
                if a is None or b is None:
                    continue
                for item_a in a.owns:
                    match = next((item_b for item_b in b.owns if regions_overlap(item_a, item_b)), None)
                    if match is not None:
                        found.append({"frame": frame, "left": left, "right": right, "left_region": item_a,
                                      "right_region": match})
                        break
    return found


def frame_scope_problems(markdown: str) -> list[str]:
    """Planner contract: a frame named by several work orders is split between them, without overlap."""
    entries = _sections(markdown)
    problems: list[str] = []
    for frame, refs in sorted(shared_frames(entries).items(), key=lambda item: int(item[0][1:])):
        for ref in refs:
            scope = parse_frame_scope(entries[ref]).get(frame)
            others = ", ".join(r for r in refs if r != ref)
            if scope is None or not scope.owns:
                problems.append(
                    f"{ref} names design frame {frame}, which {others} also name{'s' if len(refs) == 2 else ''}; give "
                    f"{ref} a `{FRAME_SCOPE_HEADING}` section with a `### {frame}` entry: `- Owns:` (the regions or "
                    f"elements of {frame} this work order builds) and `- Owned by other work orders:` (`WO-n: region` "
                    "for each region the others build), so QA judges each work order only on its own part"
                )
    for claim in overlapping_claims(entries):
        problems.append(
            f"{claim['left']} and {claim['right']} both claim the same region of design frame {claim['frame']} "
            f"(\"{claim['left_region']}\" and \"{claim['right_region']}\"); give each region of a shared frame to "
            "one work order and list it under the other's `Owned by other work orders`"
        )
    return problems


# ── Jira tickets ─────────────────────────────────────────────────────────────


def ticket_scope_sections(
    own_ref: str, own_body: str, siblings: list[dict[str, Any]], *, own_title: str = ""
) -> tuple[str, list[dict[str, Any]]]:
    """``(markdown to append, shared frames)`` for a ticket that shares frames with ``siblings``.

    ``siblings`` are ``{"ref", "title", "body"}`` of the other tickets in the same queue or
    program. The markdown has a ``## Shared frames`` note (who else builds each shared frame
    and what they cover) and, when the ticket has none yet, a ``## Frame scope`` section
    derived from its In Scope list, Files or summary. Empty when no frame is shared.
    """
    from ts_harness.implementation.frame_checklist import design_ids

    own_frames = design_ids(own_body)
    if not own_frames:
        return "", []
    shared: list[dict[str, Any]] = []
    for frame in own_frames:
        others = []
        for sibling in siblings:
            ref = str(sibling.get("ref") or "")
            if not ref or ref == own_ref or frame not in design_ids(str(sibling.get("body") or "")):
                continue
            covers = scope_items(str(sibling.get("body") or ""), title=str(sibling.get("title") or ""))
            others.append({"ref": ref, "title": str(sibling.get("title") or ""), "covers": covers})
        if others:
            shared.append({"frame": frame, "siblings": others})
    if not shared:
        return "", []
    lines = [
        SHARED_FRAMES_HEADING,
        "",
        "Other tickets running in the same queue or program build parts of these design frames. This ticket's "
        "design check judges only the part it owns (see Frame scope); differences in parts another ticket owns "
        "are notes, not defects. The whole-frame match is checked after delivery by the design conformance run.",
        "",
    ]
    for item in shared:
        lines.append(f"- {item['frame']} is also built by:")
        for other in item["siblings"]:
            covers = "; ".join(other["covers"]) or other["title"] or "(no summary)"
            lines.append(f"  - {other['ref']}: {covers}")
    text = "\n".join(lines) + "\n"
    existing = parse_frame_scope(own_body)
    owns = scope_items(own_body, title=own_title)
    missing = [item for item in shared if item["frame"] not in existing]
    if missing:
        scopes = [
            FrameScope(
                id=item["frame"],
                owns=owns,
                others=[
                    {"owner": other["ref"], "region": "; ".join(other["covers"]) or other["title"]}
                    for other in item["siblings"]
                ],
            )
            for item in missing
        ]
        text += "\n" + render_frame_scope(scopes)
    return text, shared


def ticket_overlaps(own_ref: str, own_body: str, shared: list[dict[str, Any]]) -> list[str]:
    """Warnings for siblings whose covered regions overlap this ticket's on a shared frame."""
    scopes = parse_frame_scope(own_body)
    warnings = []
    for item in shared:
        scope = scopes.get(item["frame"])
        owns = scope.owns if scope is not None and scope.owns else scope_items(own_body)
        for other in item["siblings"]:
            hit = next(((a, b) for a in owns for b in other["covers"] if regions_overlap(a, b)), None)
            if hit:
                warnings.append(
                    f"{own_ref} and {other['ref']} both seem to build \"{hit[0]}\" / \"{hit[1]}\" of design frame "
                    f"{item['frame']}; amend one work order to say who owns it"
                )
    return warnings


# ── QA: which colour mismatches fall outside the owned part ──────────────────


def _rgb(hex_text: str) -> tuple[int, int, int]:
    value = hex_text.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _mentions(line: str, colour: str, tolerance: int) -> bool:
    try:
        want = _rgb(colour)
    except ValueError:
        return False
    for found in _HEX.findall(line or ""):
        got = _rgb(found)
        if max(abs(got[i] - want[i]) for i in range(3)) <= tolerance:
            return True
    return False


def attribute_mismatch(
    colour: str,
    *,
    scope: FrameScope | None,
    shows: list[str],
    amendments: list[str],
    own_ref: str = "",
    tolerance: int = 10,
) -> dict[str, Any]:
    """``{"owned": bool, "owner"?, "why"?}`` for a design colour that differs in the capture.

    A mismatch is another ticket's when a person's scope amendment or the frame scope names that
    colour with another ticket, or when the frame checklist line that names the colour describes
    a region another ticket owns. Otherwise it is this ticket's.
    """
    for section in amendments:
        for line in section.splitlines():
            if not _mentions(line, colour, tolerance):
                continue
            refs = [ref for ref in TICKET_REF.findall(line) if ref != own_ref]
            if refs:
                return {"owned": False, "owner": refs[0], "why": "a scope amendment gives this colour to " + refs[0]}
    if scope is None or not scope.others:
        return {"owned": True}
    for other in scope.others:
        if _mentions(other.get("region", ""), colour, tolerance):
            return {"owned": False, "owner": other.get("owner", ""), "why": "the frame scope gives this colour's region to "
                    + (other.get("owner") or "another ticket")}
    for line in shows:
        if not _mentions(line, colour, tolerance):
            continue
        words = _tokens(line)
        best_own = max((len(words & _tokens(item)) for item in scope.owns), default=0)
        best_other, owner = 0, ""
        for other in scope.others:
            score = len(words & _tokens(other.get("region", "")))
            if score > best_other:
                best_other, owner = score, other.get("owner", "")
        if best_other > best_own:
            return {"owned": False, "owner": owner, "why": f"the frame checklist puts this colour on a region {owner or 'another ticket'} owns"}
    return {"owned": True}


def scope_design_checks(
    checks: list[dict[str, Any]], body: str, *, own_ref: str = "", tolerance: int = 10
) -> list[dict[str, Any]]:
    """``checks`` with each colour mismatch marked ``owned`` or given to the ticket that owns it."""
    from ts_harness.implementation.frame_checklist import parse_frame_checklist

    scopes = parse_frame_scope(body)
    amendments = amendment_sections(body)
    if not scopes and not amendments:
        return checks
    shows = {entry.id: entry.shows for entry in parse_frame_checklist(body)}
    out = []
    for check in checks:
        frame = str(check.get("id") or "")
        scope = scopes.get(frame)
        mismatches = []
        for pair in check.get("colour_mismatches") or []:
            verdict = attribute_mismatch(
                str(pair.get("design") or ""), scope=scope, shows=shows.get(frame, []), amendments=amendments,
                own_ref=own_ref, tolerance=tolerance,
            )
            mismatches.append({**pair, **verdict})
        updated = {**check, "colour_mismatches": mismatches}
        if scope is not None or any(not m.get("owned", True) for m in mismatches):
            updated["scope_note"] = (
                "This frame is shared with other tickets. A mismatch marked owned: false is in a region another "
                "ticket owns: list it as a note, not a defect."
            )
        out.append(updated)
    return out


def qa_frame_scope(body: str) -> list[dict[str, Any]]:
    """The work order's frame scope for the QA input (``[]`` when it owns every frame it names)."""
    return [scope.as_dict() for scope in parse_frame_scope(body).values()]
