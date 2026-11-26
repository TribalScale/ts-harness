"""What a pull request changed in its tests, for the code review and QA gates.

An existing test records behaviour someone asked for. When a change breaks it, an agent can make
the suite pass by rewriting the test instead of the code: "a planner can pick up any block
and Carry-over card" became "...but not a Carry-over card", and the drag from Carry-over was gone
with every check green. Reading a long patch, a reviewer misses one flipped line among hundreds.

So this lists, per test file, the test names and assertions the patch removed and added, and
pairs a removed line with an added line that reads almost the same but says the opposite (a
``reversal``). The reviewer and QA decide whether the work order asked for the new behaviour.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# Test names: it('...'), test("..."), describe(`...`), Scenario: ..., def test_...
_TEST_NAME = re.compile(
    r"""^\s*(?:(?:it|test|describe|context)(?:\.(?:only|skip|each\([^)]*\)))?\s*\(\s*(['"`])(?P<js>.+?)\1"""
    r"""|(?:Scenario(?: Outline)?|Feature|Rule)\s*:\s*(?P<gherkin>.+)"""
    r"""|(?:async\s+)?def\s+(?P<py>test_\w+))"""
)
# Assertions: expect(...), assert ..., Then/And/But steps, self.assertX(...)
_ASSERTION = re.compile(
    r"^\s*(?:await\s+)?(?:expect\s*\(|expect\.|assert\b|self\.assert\w*\(|(?:Then|And|But)\s+\S)"
)
# Word pairs whose swap turns an expectation into its opposite.
_OPPOSITES = (
    ("and", "but not"),
    ("can", "cannot"),
    ("can", "can't"),
    ("shows", "hides"),
    ("show", "hide"),
    ("visible", "hidden"),
    ("allowed", "refused"),
    ("allows", "refuses"),
    ("enabled", "disabled"),
    ("true", "false"),
    ("tobeinthedocument", "tobenull"),
    ("tobeinthedocument", "not.tobeinthedocument"),
    ("tobevisible", "tobehidden"),
    ("tobevisible", "not.tobevisible"),
    ("tobetruthy", "tobefalsy"),
    ("tobetruthy", "tobenull"),
    ("tobe('draggable')", "tobenull"),
    ("tohavelength", "tohavelength(0)"),
    ("is", "is not"),
    ("does", "does not"),
    ("should", "should not"),
)
_PAIR_SIMILARITY = 0.6
MAX_LINES_PER_FILE = 40


def _is_test_path(path: str) -> bool:
    from ts_harness.workflows.code_review import is_test_path

    return is_test_path(path)


def _diff_lines(patch: str) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    added: list[str] = []
    for line in (patch or "").splitlines():
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    return removed, added


def _test_name(line: str) -> str | None:
    match = _TEST_NAME.match(line)
    if not match:
        return None
    return (match.group("js") or match.group("gherkin") or match.group("py") or "").strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _says_opposite(old: str, new: str) -> bool:
    a, b = _norm(old).replace(" ", ""), _norm(new).replace(" ", "")
    spaced_a, spaced_b = _norm(old), _norm(new)
    for left, right in _OPPOSITES:
        for x, y in ((left, right), (right, left)):
            xs, ys = x.replace(" ", ""), y.replace(" ", "")
            if " " in x or " " in y or "(" in x or "." in x:
                if (
                    xs in a
                    and ys in b
                    and ys not in a
                    and (xs not in ys or a.count(xs) > b.count(xs))
                ):
                    return True
            elif re.search(rf"\b{re.escape(x)}\b", spaced_a) and re.search(
                rf"\b{re.escape(y)}\b", spaced_b
            ):
                if not re.search(rf"\b{re.escape(y)}\b", spaced_a) and not re.search(
                    rf"\b{re.escape(x)}\b", spaced_b
                ):
                    return True
    return False


def _pair(removed: list[str], added: list[str]) -> list[dict[str, str]]:
    """Removed and added lines that read alike but expect the opposite."""
    pairs: list[dict[str, str]] = []
    used: set[int] = set()
    for old in removed:
        best, best_ratio = None, 0.0
        for index, new in enumerate(added):
            if index in used:
                continue
            ratio = SequenceMatcher(None, _norm(old), _norm(new)).ratio()
            if ratio > best_ratio:
                best, best_ratio = index, ratio
        if best is not None and best_ratio >= _PAIR_SIMILARITY and _says_opposite(old, added[best]):
            used.add(best)
            pairs.append({"before": old.strip(), "after": added[best].strip()})
    return pairs


def test_changes(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per changed test file: removed/added test names and assertions, and likely reversals.

    Files that add tests only (no removed test names or assertions) are left out: new tests
    can't hide a regression.
    """
    out: list[dict[str, Any]] = []
    for item in files or []:
        path = str(item.get("filename") or "")
        if not _is_test_path(path) or str(item.get("status") or "") == "added":
            continue
        removed, added = _diff_lines(str(item.get("patch") or ""))
        removed_names = [name for name in map(_test_name, removed) if name]
        added_names = [name for name in map(_test_name, added) if name]
        removed_asserts = [line.strip() for line in removed if _ASSERTION.match(line)]
        added_asserts = [line.strip() for line in added if _ASSERTION.match(line)]
        if not removed_names and not removed_asserts and str(item.get("status") or "") != "removed":
            continue
        names_gone = [name for name in removed_names if name not in added_names]
        names_new = [name for name in added_names if name not in removed_names]
        reversals = _pair(names_gone, names_new) + _pair(
            [a for a in removed_asserts if a not in added_asserts],
            [a for a in added_asserts if a not in removed_asserts],
        )
        entry: dict[str, Any] = {
            "file": path,
            "pr_url": item.get("pr_url"),
            "status": item.get("status"),
            "removed_tests": names_gone[:MAX_LINES_PER_FILE],
            "added_tests": names_new[:MAX_LINES_PER_FILE],
            "removed_assertions": [a for a in removed_asserts if a not in added_asserts][
                :MAX_LINES_PER_FILE
            ],
            "added_assertions": [a for a in added_asserts if a not in removed_asserts][
                :MAX_LINES_PER_FILE
            ],
            "reversals": reversals,
        }
        if (
            any(entry[key] for key in ("removed_tests", "removed_assertions", "reversals"))
            or entry["status"] == "removed"
        ):
            out.append(entry)
    return out
