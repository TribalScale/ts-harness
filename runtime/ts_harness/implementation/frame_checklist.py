"""The frame checklist of a UI work order: what each design frame shows, and how to reach it.

A work order that builds UI names its design frames (``- Design: D1, D3``). A frame carries
behaviour the prose rarely repeats: a side panel that opens when a chip is selected, blocks
that dim instead of disappearing, the status line and time left on a card, the colour of a
row. When the work order only names the frame, the coding agent builds what the prose says,
QA compares whatever screenshot it happens to get, and the rest is never checked.

So each listed frame gets an entry under ``## Frame checklist``::

    ## Frame checklist

    ### D7 · 02 Week · Sources · Carry-over
    - Route: /hub/week?source=carryOver
    - State: the Carry-over chip is selected; test data has placed and unplaced carry-over orders
    - Shows:
      - a Carry-over panel on the right: "{n} orders · {p} placed · {u} not placed"
      - blocks that are not carry-over are dimmed, carry-over blocks stay at full strength
      - content area white (#FFFFFF), area header rows grey (#F3F4F6)

``Route`` and ``State`` say how to reach the frame, so the coding agent writes a frame
scenario for it and the harness captures it at the frame's size. ``Shows`` lists what the
frame requires, so the coding agent builds it and QA checks it. The planner contract rejects a
UI work order without an entry per frame, or with more frames than an agent call can see.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

CHECKLIST_HEADING = "## Frame checklist"
# One agent call carries at most this many images (``ts_harness.intake.design.AGENT_IMAGE_LIMIT``),
# so a work order with more frames is built and checked against only some of them.
DEFAULT_MAX_FRAMES_PER_WORK_ORDER = 6

_WO_HEADER = re.compile(r"^#{1,3}\s+(WO-\d+)\b.*$", re.IGNORECASE | re.MULTILINE)
_DESIGN_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}Design\*{0,2}\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE
)
_DESIGN_ROW = re.compile(r"\|\s*\*{0,2}Design\*{0,2}\s*\|\s*([^|\n]+)\|", re.IGNORECASE)
_REF_ID = re.compile(r"\bD(\d{1,3})\b")
_CHECKLIST = re.compile(r"^#{2,3}\s+Frame checklist\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_SECTION = re.compile(r"^#{1,2}\s+\S", re.MULTILINE)
_ENTRY = re.compile(r"^#{3,4}\s+(D\d{1,3})\b\s*[·:\-–—]?\s*(.*)$", re.MULTILINE)
_FIELD = re.compile(r"^\s*[-*]\s+\*{0,2}(Route|State|Shows)\*{0,2}\s*:\s*(.*)$", re.IGNORECASE)
_BULLET = re.compile(r"^\s{2,}[-*]\s+(.+)$")


@dataclass
class FrameEntry:
    """One frame of a work order's checklist."""

    id: str
    title: str = ""
    route: str = ""
    state: str = ""
    shows: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "route": self.route,
            "state": self.state,
            "shows": list(self.shows),
        }


def max_frames_per_work_order() -> int:
    """Frames one UI work order may name (``TS_HARNESS_MAX_FRAMES_PER_WO``)."""
    try:
        value = int(
            os.environ.get("TS_HARNESS_MAX_FRAMES_PER_WO", "") or DEFAULT_MAX_FRAMES_PER_WORK_ORDER
        )
    except ValueError:
        value = DEFAULT_MAX_FRAMES_PER_WORK_ORDER
    return max(1, value)


def design_ids(section: str) -> list[str]:
    """The design references a work order names (``- Design: D1, D3`` or a ``| Design |`` row), in order."""
    found: list[str] = []
    for match in [*_DESIGN_LINE.finditer(section or ""), *_DESIGN_ROW.finditer(section or "")]:
        for number in _REF_ID.findall(match.group(1)):
            ref = f"D{int(number)}"
            if ref not in found:
                found.append(ref)
    return found


def _checklist_text(section: str) -> str | None:
    match = _CHECKLIST.search(section or "")
    if not match:
        return None
    rest = section[match.end() :]
    end = _NEXT_SECTION.search(rest)
    return rest[: end.start()] if end else rest


def parse_frame_checklist(section: str) -> list[FrameEntry]:
    """The entries under ``## Frame checklist`` in one work order (empty when it has none)."""
    text = _checklist_text(section)
    if text is None:
        return []
    entries: list[FrameEntry] = []
    current: FrameEntry | None = None
    in_shows = False
    for line in text.splitlines():
        entry = _ENTRY.match(line)
        if entry:
            current = FrameEntry(id=f"D{int(entry.group(1)[1:])}", title=entry.group(2).strip())
            entries.append(current)
            in_shows = False
            continue
        if current is None:
            continue
        found = _FIELD.match(line)
        if found:
            name, value = found.group(1).lower(), found.group(2).strip().strip("`")
            in_shows = name == "shows"
            if name == "route":
                current.route = value
            elif name == "state":
                current.state = value
            elif value:
                current.shows.append(value)
            continue
        bullet = _BULLET.match(line)
        if bullet and in_shows:
            current.shows.append(bullet.group(1).strip())
    return entries


def work_order_frames(section: str) -> list[dict[str, Any]]:
    """The listed frames with their checklist entry (``route``/``state``/``shows`` empty when missing)."""
    entries = {entry.id: entry for entry in parse_frame_checklist(section)}
    return [(entries.get(ref) or FrameEntry(id=ref)).as_dict() for ref in design_ids(section)]


def _sections(markdown: str) -> dict[str, str]:
    text = (markdown or "").strip()
    matches = list(_WO_HEADER.finditer(text))
    return {
        match.group(1).upper(): text[
            match.start() : matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        ]
        for idx, match in enumerate(matches)
    }


def frame_checklist_problems(markdown: str) -> list[str]:
    """UI work orders whose frames the coding agent and QA could not all build and check.

    A work order that names design frames names at most ``max_frames_per_work_order()`` of
    them and gives each one a checklist entry with a route, a state and what it shows.
    """
    cap = max_frames_per_work_order()
    problems: list[str] = []
    for wo_id, section in _sections(markdown).items():
        listed = design_ids(section)
        if not listed:
            continue
        if len(listed) > cap:
            problems.append(
                f"{wo_id} names {len(listed)} design frames; an agent call carries at most {cap} images, so the "
                f"coding agent and QA would see only some of them. Split it by screen (one work order per screen, "
                f"at most {cap} frames each), with the shared shell, grid and colour tokens in a work order the "
                "screens depend on"
            )
        entries = {entry.id: entry for entry in parse_frame_checklist(section)}
        if not entries:
            problems.append(
                f"{wo_id} names design frames ({', '.join(listed)}) but has no `{CHECKLIST_HEADING}` section; give "
                "each frame a `### D<n> · <frame title>` entry with `Route:`, `State:` and `Shows:` bullets"
            )
            continue
        missing = [ref for ref in listed if ref not in entries]
        if missing:
            problems.append(f"{wo_id}'s frame checklist has no entry for {', '.join(missing)}")
        extra = [ref for ref in entries if ref not in listed]
        if extra:
            problems.append(
                f"{wo_id}'s frame checklist has entries for {', '.join(extra)}, which its `Design:` line does not "
                "name; list them there or drop the entries"
            )
        for ref in listed:
            entry = entries.get(ref)
            if entry is None:
                continue
            gaps = [
                name
                for name, value in (("Route", entry.route), ("State", entry.state))
                if not value
            ]
            if not entry.shows:
                gaps.append("Shows")
            if gaps:
                problems.append(f"{wo_id}'s checklist entry for {ref} has no {', '.join(gaps)}")
    return problems


def render_frame_checklist(frames: list[dict[str, Any]]) -> str:
    """The checklist as prompt text, for the coding agent and QA."""
    lines: list[str] = []
    for frame in frames:
        title = f" · {frame['title']}" if frame.get("title") else ""
        lines.append(f"- **{frame['id']}**{title}")
        if frame.get("route"):
            lines.append(f"  - Route: {frame['route']}")
        if frame.get("state"):
            lines.append(f"  - State: {frame['state']}")
        for item in frame.get("shows") or []:
            lines.append(f"  - Shows: {item}")
    return "\n".join(lines)


FRAME_TAG_PREFIX = "@frame-"


def frame_tag(ref_id: str) -> str:
    """The Gherkin tag of a frame scenario: ``@frame-D7``."""
    return f"{FRAME_TAG_PREFIX}{ref_id}"


def frame_scenario_rule(frames: list[dict[str, Any]]) -> str:
    """Coding prompt section: build every Shows item and add a capturable scenario per frame."""
    if not frames:
        return ""
    return (
        "Frame checklist. Each design frame below is part of this work order: build everything listed under "
        "Shows for it (panels, card content, selected and dimmed states, colours, type), even where the prose "
        "doesn't repeat it.\n"
        f"{render_frame_checklist(frames)}\n"
        "For each frame, add one Gherkin scenario tagged with the frame's tag (for example "
        f"`{frame_tag('D7')}`) to this work order's feature file. It signs in as the State's role, sets up the "
        "State's data, opens the Route, does the State's clicks and ends on that state, with no steps after it. "
        "The harness runs every frame scenario at the frame's own size and screenshots its last state, and QA "
        "compares that screenshot with the frame. A frame with no scenario, or one that ends somewhere else, "
        "fails QA.\n"
    )


_FRAME_LINK = re.compile(
    r"^\s*-\s+(D\d{1,3})\s+—\s+(.*?)(?:\s+·\s+(\d{2,5})×(\d{2,5}))?\s*$", re.MULTILINE
)
_LINK_TITLE = re.compile(r"^\[(.*)\]\(.*\)$")
DEFAULT_FRAME_SIZE = (1440, 900)


def frames_to_capture(section: str) -> list[dict[str, Any]]:
    """``{id, title, width, height}`` per listed frame, sizes from the ``## Design frames`` section.

    A frame whose size isn't known is captured at 1440×900, the desktop size most designs use.
    """
    sizes: dict[str, tuple[int, int]] = {}
    titles: dict[str, str] = {}
    for match in _FRAME_LINK.finditer(section or ""):
        ref = f"D{int(match.group(1)[1:])}"
        title = match.group(2).strip()
        link = _LINK_TITLE.match(title)
        titles[ref] = (link.group(1) if link else title).replace(" (attached image)", "")
        if match.group(3):
            sizes[ref] = (int(match.group(3)), int(match.group(4)))
    out = []
    for frame in work_order_frames(section):
        width, height = sizes.get(frame["id"], DEFAULT_FRAME_SIZE)
        out.append(
            {
                "id": frame["id"],
                "title": frame.get("title") or titles.get(frame["id"], ""),
                "width": width,
                "height": height,
            }
        )
    return out
