"""Title and description for the pull request a coding job opens.

The description follows the usual shape of a team PR: what the change does, the files it
touches, how to test it, what was verified, what is out of scope, and links back to the work
order. It is built from the work order (title, summary, acceptance tests, out of scope), the
changed files and the host verification, so it needs no extra model call. A repository's own
PR template, when it has one, is added at the end for the reviewer to fill in.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ts_harness.implementation.backends.base import CodingRequest, attribution_markdown, security_markdown
from ts_harness.implementation.work_order_context import extract_work_order_section

MAX_FILES = 40
MAX_TESTS = 15
_TEMPLATES = (
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
)
# A work order (WO-005) or a Jira ticket (THD-33) heading: "# THD-33 Show the comment count".
_HEADING = re.compile(r"^#{1,3}\s+(?P<id>[A-Z][A-Z0-9_]*-\d+)\b[\s:.\-–—]*(?P<title>.*)$", re.IGNORECASE | re.MULTILINE)
# "## Requirements" in a work order; a Jira description has the same names on a line of their own.
_SECTION = re.compile(
    r"^(?:#{2,4}\s+(?P<name>.+?)|(?P<bare>summary|description|requirements|acceptance criteria|e2e acceptance tests"
    r"|acceptance tests|out of scope|non-goals|technical constraints|context graph|dependencies))\s*:?\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _work_order(request: CodingRequest) -> str:
    body = request.work_order_body or ""
    return extract_work_order_section(body, request.work_order_id) or body


def _sections(markdown: str) -> dict[str, str]:
    matches = list(_SECTION.finditer(markdown))
    out: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        out[(match.group("name") or match.group("bare")).strip().lower()] = markdown[match.end() : end].strip()
    return out


def _first_paragraph(markdown: str) -> str:
    """The text between the title and the first section: a ticket's opening sentence."""
    body = re.sub(r"^#\s+.*\n?", "", markdown.strip(), count=1).strip()
    first = _SECTION.search(body)
    lead = body[: first.start()] if first else body
    paragraph = lead.strip().split("\n\n", 1)[0].strip()
    return "" if paragraph.lower().startswith(("repo:", "wo:", "program:")) else paragraph


def _section(sections: dict[str, str], *names: str) -> str:
    for name in names:
        for key, value in sections.items():
            if key == name or key.startswith(name):
                return value
    return ""


def _bullets(text: str) -> list[str]:
    return [line.lstrip("-*0123456789. ").strip() for line in text.splitlines() if re.match(r"^\s*([-*]|\d+\.)\s+", line)]


def pr_title(request: CodingRequest) -> str:
    match = next(
        (m for m in _HEADING.finditer(_work_order(request)) if m.group("id").upper() == request.work_order_id.upper()),
        None,
    ) or _HEADING.search(_work_order(request))
    title = (match.group("title") if match else "").strip().strip("#").strip()
    return f"{request.work_order_id}: {title}" if title else f"{request.work_order_id}: implement work order"


def _short_command(command: str, repo_dir: Path | None) -> str:
    """Commands as a reviewer would type them: paths relative to the repository."""
    text = str(command or "")
    if repo_dir is not None:
        root = str(repo_dir).rstrip("/")
        text = re.sub(rf"env -C {re.escape(root)}/(\S+) ", r"(in \1) ", text)
        text = text.replace(root + "/", "").replace(root, ".")
    text = re.sub(r"(?:\S*/)?node_modules/\.bin/", "", text)
    return text if len(text) <= 160 else text[:157] + "..."


def _verification(verification: dict[str, Any], repo_dir: Path | None = None) -> list[str]:
    checks = [c for c in verification.get("checks") or [] if isinstance(c, dict)]
    if not checks:
        return []
    lines = ["| Check | Command | Result |", "|---|---|---|"]
    for check in checks:
        code = check.get("exit_code")
        result = "passed" if code == 0 else (f"exit {code}" if code is not None else "not run")
        lines.append(f"| {check.get('check')} | `{_short_command(str(check.get('command') or ''), repo_dir)}` | {result} |")
    if verification.get("skipped"):
        lines += ["", "Not run: " + ", ".join(verification["skipped"]) + " (not required for this repo or work order)."]
    return lines


def _template(repo_dir: Path | None) -> str:
    if repo_dir is None:
        return ""
    for name in _TEMPLATES:
        path = repo_dir / name
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
    return ""


def pr_body(
    request: CodingRequest,
    *,
    changed_files: list[str],
    verification: dict[str, Any] | None = None,
    security: dict[str, Any] | None = None,
    docs_written: bool = False,
    repo_dir: Path | None = None,
    screenshots: bool = False,
    out_of_scope_files: list[str] | None = None,
) -> str:
    sections = _sections(_work_order(request))
    parts: list[str] = []

    summary = _section(sections, "summary", "goal", "overview", "description") or _first_paragraph(_work_order(request))
    parts += ["## Summary", "", summary or f"Implements work order `{request.work_order_id}`.", ""]

    beyond = [f for f in out_of_scope_files or [] if f]
    files = [f for f in changed_files if f]
    if beyond:
        parts += [
            "## Beyond the work order's files",
            "",
            "These changed files are not in the work order's Files list. The code review gate judges whether they belong.",
            "",
            *[f"- `{name}`" for name in beyond[:MAX_FILES]],
            "",
        ]
    if files:
        parts += [f"## Changes ({len(files)} files)", ""]
        parts += [f"- `{name}`" for name in files[:MAX_FILES]]
        if len(files) > MAX_FILES:
            parts.append(f"- and {len(files) - MAX_FILES} more")
        if docs_written:
            parts.append("- Planning specs (Refinery, Foundry, Planner) under `docs/`, approved before coding")
        parts.append("")

    tests = _bullets(_section(sections, "e2e acceptance tests", "acceptance tests", "acceptance criteria"))
    parts += ["## How to test", ""]
    if tests:
        parts.append("Each acceptance test from the work order:")
        parts.append("")
        parts += [f"- [ ] {test}" for test in tests[:MAX_TESTS]]
        if len(tests) > MAX_TESTS:
            parts.append(f"- [ ] and {len(tests) - MAX_TESTS} more in the work order")
    else:
        parts.append("Run the checks below; the work order lists no separate acceptance tests.")
    if screenshots:
        parts += ["", "Screenshots from the Playwright tests are in a comment below."]
    parts.append("")

    checks = _verification(verification or {}, repo_dir)
    if checks:
        result = (verification or {}).get("result")
        parts += [f"## Verification ({result})" if result else "## Verification", "", *checks, ""]

    out_of_scope = _bullets(_section(sections, "out of scope", "non-goals"))
    if out_of_scope:
        parts += ["## Out of scope", "", *(f"- {item}" for item in out_of_scope), ""]

    refs = [ref for ref in request.design_refs or [] if isinstance(ref, dict) and ref.get("id")]
    if refs:
        parts += ["## Design", ""]
        for ref in refs:
            link = f" ([source]({ref['url']}))" if str(ref.get("url") or "").startswith("http") else ""
            parts.append(f"- {ref['id']}: {ref.get('title') or 'design reference'}{link}")
        parts.append("")

    body = "\n".join(parts)
    body += f"\n## Work order\n\n- `{request.work_order_id}`\n"
    depends = _section(sections, "context graph", "dependencies")
    if depends:
        body += "".join(f"- {item}\n" for item in _bullets(depends))
    body += attribution_markdown(request).replace("\n## Attribution\n\n", "")
    body += security_markdown(security or {})

    template = _template(repo_dir)
    if template:
        body += "\n## Repository checklist\n\n" + template + "\n"

    body += (
        "\n---\n"
        "Opened by TS Harness (OpenCode). Code review and QA post their results on this PR; "
        "a human merges, and the delivery program resumes after the merge.\n"
    )
    return body
