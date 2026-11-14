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
_FR_DEFINED = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(?:\*\*)?(FR-\d+)\b", re.MULTILINE)
_DOD_DEFINED = re.compile(r"^\s*[-*]\s*(?:\*\*)?(DoD-\d+)\b", re.MULTILINE)
_DOD_REF = re.compile(r"\bDoD-\d+\b")
_IDS = re.compile(r"\b(?:FR|DoD)-\d+\b")
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


def _is_na(body: str) -> bool:
    return body.strip().lower().startswith("n/a")


def _sample(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def check_prd(markdown: str) -> list[str]:
    """Return findings for a PRD in the prd-drafter template; an empty list means none."""
    text = (markdown or "").strip()
    if not text:
        return ["The PRD is empty."]
    plain = _strip_code(text)
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

    placeholders = sorted(
        {f"<{match.group(1).strip()}>" for match in _PLACEHOLDER.finditer(plain)
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
    for line in requirements.splitlines():
        match = _FR_LINE.search(line)
        if not match or "~~" in line:
            continue
        fr = match.group(1)
        if "specified in" not in line.lower():
            no_spec.append(fr)
        if "verified by" not in line.lower():
            no_verify.append(fr)
        else:
            bad_refs.update(ref for ref in _DOD_REF.findall(line) if dod_ids and ref not in dod_ids)
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

    if dod_body and not _is_na(dod_body) and not 5 <= len(dod_ids) <= 10:
        findings.append(f"Definition of Done has {len(dod_ids)} items; it needs 5 to 10.")

    questions = _section(sections, "Open Questions") or ""
    ownerless = [
        line.strip(" -*[]xX").strip()[:60]
        for line in questions.splitlines()
        if re.match(r"^\s*[-*]\s", line) and "owner" not in line.lower() and not _is_na(line.strip(" -*"))
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
