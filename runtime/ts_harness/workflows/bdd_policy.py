"""Whether a repository's work is tested with Gherkin acceptance scenarios (BDD), decided once.

Verification used to decide from whatever the repository held at the time of each coding job,
so the first ticket's choice of test runner decided BDD for every later ticket. Now each
program decides per repository when it first sees the project (``project_repositories[].bdd``,
kept in the program's artifacts) from the project setting (System → Projects → Testing):

- ``always``: Gherkin in every repository.
- ``never``: the repository's own test runner.
- ``auto`` (default): Gherkin for a repository with no tests yet or that already uses Gherkin;
  the repository's own runner when it has tests and no Gherkin.

Every work order then carries a ``## Tests`` section (``BDD: required`` or ``BDD: off``), which
Jira tickets copy, and verification and the coding agent follow it. A repository that needs
BDD but has no Gherkin runner yet gets one with its first ticket.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any

MODES = ("auto", "always", "never")
TESTS_HEADING = "## Tests"
_MARKER = re.compile(r"^\s*(?:[-*]\s+)?\*{0,2}BDD\*{0,2}\s*:\s*\*{0,2}\s*(required|off)\b", re.IGNORECASE | re.MULTILINE)
_TEST_FILES = (
    "*.test.*", "*.spec.*", "*_test.go", "*_test.py", "test_*.py", "*Test.java", "*Tests.java", "*Test.kt",
    "*_spec.rb", "tests/*", "test/*", "__tests__/*", "spec/*",
)
_BDD_SCRIPTS = ("test:bdd", "cucumber", "bdd")
_BDD_DEPS = ("@cucumber/cucumber", "cucumber", "jest-cucumber", "playwright-bdd", "pytest-bdd", "behave")


def mode_of(value: Any) -> str:
    mode = str(value or "auto").strip().lower()
    return mode if mode in MODES else "auto"


def _basename_or_path_matches(path: str, pattern: str) -> bool:
    if "/" in pattern:
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, "*/" + pattern)
    return fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)


def inspect_files(paths: list[str], package_json: str | None = None) -> dict[str, bool]:
    """What a repository's file list says about its tests: ``has_tests`` and ``has_gherkin``."""
    files = [p for p in paths if "node_modules/" not in p]
    scripts: dict[str, Any] = {}
    deps: dict[str, Any] = {}
    if package_json:
        try:
            data = json.loads(package_json)
            scripts = data.get("scripts") or {}
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        except (ValueError, AttributeError):
            pass
    has_features = any(p.endswith(".feature") for p in files)
    runner_script = any(name in scripts for name in _BDD_SCRIPTS) or any(name in deps for name in _BDD_DEPS)
    behave = "behave.ini" in files or any(p.startswith("features/steps/") and p.endswith(".py") for p in files)
    has_tests = any(_basename_or_path_matches(p, pattern) for p in files for pattern in _TEST_FILES) or has_features
    test_script = str(scripts.get("test") or "")
    if test_script and "no test specified" not in test_script:
        has_tests = True
    return {"has_tests": has_tests, "has_gherkin": has_features or runner_script or behave, "has_gherkin_runner": runner_script or behave}


def decide(mode: str, facts: dict[str, bool]) -> tuple[bool, str]:
    """``(use_bdd, reason)`` for a repository from the project setting and its test facts."""
    mode = mode_of(mode)
    if mode == "always":
        return True, "project setting: always"
    if mode == "never":
        return False, "project setting: never"
    if facts.get("has_gherkin"):
        return True, "the repository already uses Gherkin"
    if not facts.get("has_tests"):
        return True, "the repository has no tests yet"
    return False, "the repository has its own tests and no Gherkin"


def marker(body: str) -> bool | None:
    """``BDD: required`` → True, ``BDD: off`` → False, no line → None (decide from the repository)."""
    match = _MARKER.search(str(body or ""))
    if not match:
        return None
    return match.group(1).lower() == "required"


def tests_section(use_bdd: bool, *, needs_runner: bool = False) -> str:
    """The ``## Tests`` section a work order (and its Jira ticket) carries."""
    if use_bdd:
        lines = [
            TESTS_HEADING,
            "",
            "BDD: required",
            "",
            "- Acceptance: one Gherkin scenario per acceptance criterion, in a feature file named after the "
            "ticket key (for example `features/PROJ-12-short-name.feature`), run by the repository's BDD "
            "suite (`npm run test:bdd` or the stack's equivalent).",
        ]
        if needs_runner:
            lines.append(
                "- This repository has no Gherkin runner yet: add the lightest one for its stack in this change "
                "(Cucumber.js or playwright-bdd for Node, pytest-bdd or behave for Python, godog for Go) with a "
                "`test:bdd` script or equivalent, and run it in CI."
            )
    else:
        lines = [
            TESTS_HEADING,
            "",
            "BDD: off",
            "",
            "- Acceptance: cover each acceptance criterion with the repository's own tests (an end-to-end or "
            "integration test where it has them), named after the criterion. Do not add Gherkin.",
        ]
    lines.append("- Unit: unit tests for every new or changed rule, including its error cases.")
    return "\n".join(lines) + "\n"


def repository_policy(entry: dict[str, Any]) -> bool | None:
    """The frozen decision on a ``project_repositories`` entry, if any."""
    value = entry.get("bdd")
    return value if isinstance(value, bool) else None


def with_tests_sections(markdown: str, repositories: list[dict[str, Any]] | None) -> str:
    """Work orders with a ``## Tests`` section from their repository's decision.

    A work order names its repository with a ``Repository: owner/name`` line; with one
    repository, every work order is for it. Work orders that already say ``BDD:`` keep it.
    Running it again changes nothing.
    """
    from ts_harness.implementation.work_order_context import _REPOSITORY_LINE, _WO_HEADER

    text = str(markdown or "")
    repos = [r for r in repositories or [] if isinstance(r, dict) and repository_policy(r) is not None]
    if not text or not repos:
        return text
    by_name = {str(r.get("full_name") or "").lower(): r for r in repos}
    matches = list(_WO_HEADER.finditer(text))
    if not matches:
        return text
    pieces = [text[: matches[0].start()]]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end]
        named = _REPOSITORY_LINE.search(section)
        repo = by_name.get(named.group(1).lower()) if named else (repos[0] if len(repos) == 1 else None)
        if repo is None or marker(section) is not None:
            pieces.append(section)
            continue
        body, trailing = section.rstrip(), section[len(section.rstrip()) :]
        use = bool(repository_policy(repo))
        block = tests_section(use, needs_runner=use and not repo.get("has_gherkin_runner"))
        pieces.append(body + "\n\n" + block + (trailing if trailing.count("\n") > 1 else "\n"))
    return "".join(pieces)
