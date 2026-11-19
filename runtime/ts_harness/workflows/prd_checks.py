"""Mechanical checks on a drafted PRD (prd-drafter template).

The drafter's self-check is a request to the model; these checks run on the returned
markdown so the reviewer sees what is still missing. They never block the draft: the
findings travel on the task output as ``prd_checks`` and are shown next to the draft.
"""

from __future__ import annotations

import re

MAX_FINDINGS = 12

REQUIRED_SECTIONS = (
    "Problem Statement",
    "Goals",
    "Non-Goals",
    "Decisions",
    "Assumptions",
    "Requirements",
    "Definition of Done",
    "Success Metrics",
    "Open Questions",
)
# Sections later stages copy or test against; a stated time or limit must reach one of them.
CARRIED_SECTIONS = ("Requirements", "Technical Constraints", "Definition of Done", "Non-Functional Requirements")
# Sections where a vague word means a requirement nobody can test.
TESTABLE_SECTIONS = ("Requirements", "Definition of Done", "Non-Functional Requirements")
VAGUE_WORDS = (
    "fast",
    "quick",
    "quickly",
    "easy",
    "easily",
    "intuitive",
    "user-friendly",
    "seamless",
    "seamlessly",
    "robust",
    "scalable",
    "efficient",
    "as needed",
    "etc",
)
_HTML_TAGS = {"br", "b", "i", "u", "em", "strong", "sub", "sup", "kbd", "details", "summary", "code", "p"}

_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_PLACEHOLDER = re.compile(r"<\s*/?\s*([A-Za-z][^<>\n]{0,80})>")
_UNRESOLVED = re.compile(r"\b(TBD|TBC|TODO|FIXME)\b")
_VAGUE = re.compile(r"\b(" + "|".join(re.escape(word) for word in VAGUE_WORDS) + r")\b", re.IGNORECASE)
_FR_LINE = re.compile(r"\*\*(FR-\d+)\*\*")


def _requirement_blocks(requirements: str) -> list[tuple[str, str]]:
    """``(first line, whole requirement)`` per FR item: its line up to the next FR line."""
    blocks: list[tuple[str, list[str]]] = []
    for line in requirements.splitlines():
        if _FR_LINE.search(line):
            blocks.append((line, [line]))
        elif blocks:
            blocks[-1][1].append(line)
    return [(first, "\n".join(lines)) for first, lines in blocks]
_FR_DEFINED = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(?:\*\*)?(FR-\d+)\b", re.MULTILINE)
_DOD_DEFINED = re.compile(r"^\s*[-*]\s*(?:\*\*)?(DoD-\d+)\b", re.MULTILINE)
_DOD_REF = re.compile(r"\bDoD-\d+\b")
_IDS = re.compile(r"\b(?:FR|DoD)-\d+\b")
# Specifics a requester states that must survive into Requirements: times of day and
# quantities with a unit. Plain counts and percentages are context, not requirements.
_SPECIFIC = re.compile(
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b"
    r"|\b(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s?(?:ms|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|"
    r"KB|MB|GB|TB|requests?|rows|records|items|characters|chars)\b",
    re.IGNORECASE,
)
# The template's own header fields are not placeholders the drafter forgot.
_HEADER_LINE = re.compile(r"^\*\*(?:Author|Linked epic|Profile|Status|Last updated):\*\*.*$", re.MULTILINE)
_PRIORITY = re.compile(r"\((Must|Should|Could)\)")


def _strip_code(markdown: str) -> str:
    return _INLINE_CODE.sub("", _FENCE.sub("", markdown))


def _sections(markdown: str) -> dict[str, str]:
    """Level-2 sections by normalized heading ("Non-Functional Requirements (Full)" -> without the tag)."""
    sections: dict[str, str] = {}
    current = ""
    lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(lines).strip()
            current = re.sub(r"\s*\((?:full|lite)\)\s*$", "", line[3:].strip(), flags=re.IGNORECASE)
            lines = []
        elif current:
            lines.append(line)
    if current:
        sections[current] = "\n".join(lines).strip()
    return sections


def _section(sections: dict[str, str], name: str) -> str | None:
    for heading, body in sections.items():
        if heading.lower() == name.lower():
            return body
    return None


_PROFILE_LINE = re.compile(r"^\*\*Profile:\*\*\s*(\w+)", re.MULTILINE | re.IGNORECASE)


def _is_full_profile(markdown: str) -> bool:
    match = _PROFILE_LINE.search(markdown or "")
    return bool(match and match.group(1).lower() == "full")


def _table(body: str) -> tuple[list[str], list[dict[str, str]]]:
    """The first Markdown table in ``body``: lower-cased header and its rows as dicts."""
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if header:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not header:
            header = [cell.lower() for cell in cells]
        elif not all(set(cell) <= set("-: ") for cell in cells):
            rows.append(dict(zip(header, cells)))
    return header, rows


def _column(header: list[str], *names: str) -> str | None:
    return next((h for h in header if any(h.startswith(name) for name in names)), None)


def _full_framing_findings(sections: dict[str, str]) -> list[str]:
    """A Full PRD frames the project: problems per persona with evidence, goals with a measure,
    baseline and target, non-goals with a reason (prd-drafter, "Framing a Full PRD")."""
    findings: list[str] = []
    problem = _section(sections, "Problem Statement") or ""
    if problem and not _is_na(problem):
        header, rows = _table(problem)
        if not _column(header, "persona") or len(rows) < 2:
            findings.append(
                "Problem Statement (Full): add a table of the problem per persona (Persona | Problem | Evidence), "
                "at least two personas."
            )
        elif _column(header, "evidence") and any(not row.get(_column(header, "evidence") or "", "") for row in rows):
            findings.append("Problem Statement (Full): every persona's problem needs its evidence and source.")
        missing = [label for label in ("Current state", "Why now", "Cost of doing nothing") if f"**{label}:**" not in problem]
        if missing:
            findings.append(f"Problem Statement (Full): add {', '.join(f'**{m}:**' for m in missing)}.")
    goals = _section(sections, "Goals") or ""
    if goals and not _is_na(goals):
        header, rows = _table(goals)
        wanted = {"measure": _column(header, "measure"), "baseline": _column(header, "baseline"), "target": _column(header, "target")}
        if not rows or not all(wanted.values()):
            findings.append(
                "Goals (Full): write them as a table: Goal | Who benefits | Measure | Baseline | Target | By when | Serves."
            )
        else:
            incomplete = [
                (row.get(header[0]) or "?")[:40]
                for row in rows
                if any(not row.get(column or "", "").strip(" -—") for column in wanted.values())
            ]
            if incomplete:
                findings.append(f"Goals (Full): a measure, baseline and target for every goal: {_sample(incomplete)}.")
    if non_goals_body := (_section(sections, "Non-Goals") or ""):
        if not _is_na(non_goals_body):
            header, rows = _table(non_goals_body)
            reason = _column(header, "reason", "why")
            if not rows or not reason:
                findings.append("Non-Goals (Full): write them as a table: Item | Category | Reason | Revisit.")
            else:
                unexplained = [(row.get(header[0]) or "?")[:40] for row in rows if not row.get(reason, "").strip(" -—")]
                if unexplained:
                    findings.append(f"Non-Goals (Full): give the reason for {_sample(unexplained)}.")
    return findings


def _is_na(body: str) -> bool:
    return body.strip().lower().startswith("n/a")


def _sample(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _normalize_specific(value: str) -> str:
    value = re.sub(r"[\s,]+", "", value.lower())
    return re.sub(r"^0(\d:)", r"\1", value)


def stated_specifics(text: str) -> list[str]:
    """Times of day and quantities with a unit in the requester's words, in order, once each."""
    seen: dict[str, str] = {}
    for match in _SPECIFIC.finditer(_strip_code(text or "")):
        seen.setdefault(_normalize_specific(match.group(0)), match.group(0).strip())
    return list(seen.values())


def check_prd(markdown: str, *, stated: str = "") -> list[str]:
    """Return findings for a PRD in the prd-drafter template; an empty list means none.

    ``stated`` is what the requester wrote (request, answers, attached sources). Each time
    of day and quantity with a unit in it must appear in Requirements or Technical
    Constraints, because later stages copy only those sections.
    """
    text = (markdown or "").strip()
    if not text:
        return ["The PRD is empty."]
    plain = _strip_code(text)
    header_free = _HEADER_LINE.sub("", plain)
    sections = _sections(plain)
    findings: list[str] = []

    missing = [name for name in REQUIRED_SECTIONS if _section(sections, name) is None]
    if missing:
        findings.append(f"Missing sections: {', '.join(missing)}.")
    empty = [
        name
        for name in REQUIRED_SECTIONS
        if (body := _section(sections, name)) is not None and not body
    ]
    if empty:
        findings.append(f"Empty sections (write N/A and the reason if they do not apply): {', '.join(empty)}.")
    non_goals = _section(sections, "Non-Goals")
    if non_goals and _is_na(non_goals):
        findings.append("Non-Goals says N/A; name at least one thing this release does not do.")
    if _is_full_profile(text):
        findings.extend(_full_framing_findings(sections))

    placeholders = sorted(
        {f"<{match.group(1).strip()}>" for match in _PLACEHOLDER.finditer(header_free)
         if match.group(1).split()[0].strip("/").lower() not in _HTML_TAGS}
    )
    if placeholders:
        findings.append(f"Template placeholders left: {_sample(placeholders)}.")
    unresolved = sorted({match.group(1) for match in _UNRESOLVED.finditer(plain)})
    if unresolved:
        findings.append(f"Unresolved markers: {', '.join(unresolved)}. Move each to Open Questions with an owner.")

    vague: list[str] = []
    for name in TESTABLE_SECTIONS:
        body = _section(sections, name)
        if not body:
            continue
        for line in body.splitlines():
            # A number makes the word testable; requirement and DoD ids do not count.
            if re.search(r"\d", _IDS.sub("", line)):
                continue
            for match in _VAGUE.finditer(line):
                vague.append(f'"{match.group(1).lower()}" in {name}')
    if vague:
        findings.append(f"Vague wording without a number or behaviour: {_sample(sorted(set(vague)))}.")

    requirements = _section(sections, "Requirements") or ""
    fr_ids = _FR_DEFINED.findall(requirements)
    duplicates = sorted({fr for fr in fr_ids if fr_ids.count(fr) > 1})
    if duplicates:
        findings.append(f"Requirement ids used more than once: {', '.join(duplicates)}.")
    dod_body = _section(sections, "Definition of Done") or ""
    dod_ids = set(_DOD_DEFINED.findall(dod_body))
    no_spec: list[str] = []
    no_verify: list[str] = []
    no_priority: list[str] = []
    bad_refs: set[str] = set()
    for line, block in _requirement_blocks(requirements):
        match = _FR_LINE.search(line)
        if not match or "~~" in line:
            continue
        fr = match.group(1)
        # A long requirement lists its details as bullets; its links may sit on any line of it.
        if "specified in" not in block.lower():
            no_spec.append(fr)
        if "verified by" not in block.lower():
            no_verify.append(fr)
        else:
            links = " ".join(row for row in block.splitlines() if "verified by" in row.lower())
            bad_refs.update(ref for ref in _DOD_REF.findall(links) if dod_ids and ref not in dod_ids)
        if not _PRIORITY.search(line):
            no_priority.append(fr)
    if requirements and not _is_na(requirements) and not fr_ids:
        findings.append("Requirements has no numbered FR-n items.")
    if no_spec:
        findings.append(f"No 'Specified in' on {_sample(no_spec)}.")
    if no_verify:
        findings.append(f"No 'Verified by' on {_sample(no_verify)}; it will ship untested.")
    if bad_refs:
        findings.append(f"'Verified by' cites DoD items that do not exist: {_sample(sorted(bad_refs))}.")
    if no_priority:
        findings.append(f"No priority (Must, Should or Could) on {_sample(no_priority)}.")

    if stated:
        carried = " ".join(_section(sections, name) or "" for name in CARRIED_SECTIONS)
        carried_norm = {_normalize_specific(m.group(0)) for m in _SPECIFIC.finditer(carried)}
        dropped = [item for item in stated_specifics(stated) if _normalize_specific(item) not in carried_norm]
        if dropped:
            findings.append(
                "Stated times and limits not found in Requirements, Technical Constraints or Definition "
                f"of Done (fine if they are only context): {_sample(dropped, 5)}."
            )

    if dod_body and not _is_na(dod_body) and not 5 <= len(dod_ids) <= 10:
        findings.append(f"Definition of Done has {len(dod_ids)} items; it needs 5 to 10.")

    questions = _section(sections, "Open Questions") or ""
    ownerless = [
        line.strip(" -*[]xX").strip()[:60]
        for line in questions.splitlines()
        if re.match(r"^\s*[-*]\s", line)
        and "owner" not in line.lower()
        and not _is_na(line.strip(" -*[]xX"))
        and line.strip(" -*[]xX.").lower() not in {"none", "no open questions"}
    ]
    if ownerless:
        findings.append(f"Open questions without an owner: {_sample(ownerless, 2)}.")

    return findings[:MAX_FINDINGS]


def format_prd_checks(findings: list[str]) -> str:
    """Channel text for the reviewer; empty when there is nothing to show."""
    if not findings:
        return ""
    lines = "\n".join(f"- {finding}" for finding in findings)
    noun = "issue" if len(findings) == 1 else "issues"
    return f"Automatic PRD checks found {len(findings)} {noun}:\n{lines}"
