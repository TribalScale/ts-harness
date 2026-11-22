"""Pre-push review: a stronger model reviews the verified diff before it is pushed.

A gate rejection costs a full round trip (push, CI, the review gate, a new coding job), about
20-30 minutes. Inside the coding job, the same review takes a few minutes and its findings
are fixed locally. The reviewer uses the gate's own checklist and blocking rule
(``governed-code-review``, ``code_review.is_blocking``), so what it passes is what the gate
is likely to pass. The gate still runs: it is a different model and the final word.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_ROUNDS = 2
OFF_VALUES = {"off", "none", "false", "0"}


def review_model() -> str | None:
    """The pre-push reviewer (``TS_HARNESS_PRE_PUSH_REVIEW_MODEL``, else the guided model); None when off."""
    setting = (os.environ.get("TS_HARNESS_PRE_PUSH_REVIEW_MODEL") or "").strip()
    if setting.lower() in OFF_VALUES:
        return None
    if setting:
        return setting
    try:
        from ts_harness.workflows.revision_guidance import guided_coding_model

        return guided_coding_model()
    except Exception:  # noqa: BLE001 - no reviewer configured
        return None


def review_rounds() -> int:
    """Fix rounds after a blocking pre-push review (``TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS``, default 2)."""
    try:
        return max(0, min(5, int(os.environ.get("TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS") or DEFAULT_ROUNDS)))
    except ValueError:
        return DEFAULT_ROUNDS


DEFAULT_BUDGET_SECONDS = 1500


def review_budget_seconds() -> int:
    """Time the pre-push review may take before it stops starting new rounds
    (``TS_HARNESS_PRE_PUSH_REVIEW_BUDGET_SECONDS``, default 25 minutes; 0 = no limit)."""
    try:
        return max(0, int(os.environ.get("TS_HARNESS_PRE_PUSH_REVIEW_BUDGET_SECONDS") or DEFAULT_BUDGET_SECONDS))
    except ValueError:
        return DEFAULT_BUDGET_SECONDS


def is_blocking(finding: dict[str, Any]) -> bool:
    from ts_harness.workflows.code_review import is_blocking as gate_blocking

    return gate_blocking(finding)


REVIEW_PROMPT = """You are the code reviewer for work order {work_order_id}. The change is committed on the
current branch and its tests pass. Review it before it is pushed, as the pull request's code
review gate will. Do not edit, create or delete any file in the repository, and do not commit.

See the change with `git diff {base_ref}...HEAD` (and `git diff --stat {base_ref}...HEAD`).
Read whole files where you need the code around a hunk. Do not run the full test suite; it
already passed. You may run a single test to check a doubt.

Work order:
{work_order_body}
{extra}
Trace every Requirement and Acceptance Test to the code that implements it, and evaluate the
code by hand with the example values and boundaries the work order gives. Then check every
changed file:

- correctness: logic matches the work order, edge cases, null safety, error handling, off-by-one, concurrency
- security: no secrets, input validation, injection, authn/authz checks, unsafe deserialization
- scope: changes stay inside In Scope; nothing from Out of Scope; no unrelated refactors
- tests: new logic has unit tests (normal case and each error or branch the change adds); assertions check outcomes
- maintainability: naming, dead code, duplication, the repo's existing patterns

Then apply these deep checks wherever the change touches them; they are what the gate most
often finds after the tests pass:
- input parsing and coercion: null, empty or whitespace turned into 0/false/"" (z.coerce.number(),
  Number(x), parseInt), numeric strings, NaN, negatives, dates and time zones; malformed input
  must fail loudly, not overwrite good data
- storage precision: values compared or reported before rounding to what the database stores
  (decimal scale, integer columns, string lengths), so an identical repeat read looks like a change
- change detection and idempotency: an identical repeat input causes no change, log, event or hook
  call; every kind of data that can change (related or reference data too) reaches the hooks or
  events that depend on it
- partial failure: transactions and rollback cover every write; retries do not duplicate
- concurrency and ordering: concurrent runs, out-of-order events, reads outside the transaction
- error paths, boundaries and permissions the work order states

Severity: `blocker` (broken functionality, data loss, a security hole), `major` (a real defect
that must be fixed before merge), `minor` (worth fixing, not a merge risk), `nit` (style).
A wrong result for a case the work order states is at least `major` `correctness`. New business
logic with no unit test is a `blocker` `tests` finding. Hardening the work order does not ask
for is `minor` at most. When the work order contradicts itself, report one `minor` finding
quoting both statements instead of blocking.

Report every blocking issue in one pass: check the whole diff against every requirement, error
message, permission, edge case and failure path before answering. In each blocking message, say
the fix you expect. Do not invent findings; an empty list is a valid review.

Write your review as JSON to {output_path} (this file is outside the repository; write nothing else):
{{"summary": "one or two sentences",
  "findings": [{{"severity": "blocker|major|minor|nit",
                 "category": "correctness|security|scope|tests|maintainability",
                 "file": "path/in/repo", "line": 0, "message": "what is wrong and the fix you expect"}}]}}
"""


def build_review_prompt(
    *,
    work_order_id: str,
    work_order_body: str,
    base_ref: str,
    output_path: Path,
    review_feedback: str | None = None,
    human_change_requests: list[str] | None = None,
) -> str:
    extra: list[str] = []
    if human_change_requests:
        extra.append(
            "\nA human reviewer asked for these changes; they amend the work order (the latest wins). "
            "Never flag code for doing what they asked:\n" + "\n".join(f"- {item}" for item in human_change_requests)
        )
    if review_feedback:
        extra.append(
            "\nThis is a revision. The PR's code review raised the findings below; check each one is fully "
            "fixed, including the code around it, as well as reviewing the whole diff:\n" + review_feedback.strip()[:6000]
        )
    return REVIEW_PROMPT.format(
        work_order_id=work_order_id,
        work_order_body=(work_order_body or "").strip()[:20000],
        base_ref=base_ref,
        output_path=str(output_path),
        extra="\n".join(extra) + ("\n" if extra else ""),
    )


def parse_review(path: Path) -> dict[str, Any] | None:
    """The review the model wrote, or None when it wrote nothing usable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return None
    findings = []
    for item in data["findings"]:
        if not isinstance(item, dict) or not str(item.get("message") or "").strip():
            continue
        findings.append(
            {
                "severity": str(item.get("severity") or "minor").lower(),
                "category": str(item.get("category") or "correctness").lower(),
                "file": str(item.get("file") or ""),
                "line": item.get("line") if isinstance(item.get("line"), int) else 0,
                "message": str(item["message"]).strip(),
            }
        )
    return {"summary": str(data.get("summary") or "").strip(), "findings": findings}


def finding_line(finding: dict[str, Any]) -> str:
    where = finding.get("file") or ""
    if where and finding.get("line"):
        where = f"{where}:{finding['line']}"
    prefix = f"[{finding.get('severity')}/{finding.get('category')}]"
    return f"- {prefix} {where} {finding.get('message')}".replace("  ", " ")


def build_fix_prompt(work_order_id: str, findings: list[dict[str, Any]], *, skills_materialized: bool = False) -> str:
    lines = [
        f"A code review of your change for {work_order_id} found problems that would block the pull request.",
        "Fix every one of them in this pass, with a test that proves each fix. Keep the rest of the change as it is,",
        "stay inside the work order's scope, and run the tests for the code you touch before finishing.",
        "",
        "Blocking findings:",
        *[finding_line(item) for item in findings],
    ]
    if skills_materialized:
        lines += ["", "Follow the project skills under .opencode/skills as before."]
    return "\n".join(lines)


def summary_record(rounds: list[dict[str, Any]], *, model: str | None) -> dict[str, Any]:
    """What the pre-push review did, for the coding result and the live view.

    Each round is ``{"status", "summary", "findings"}``; the status is ``fixed`` (its blocking
    findings were fixed and verified), ``pass`` (nothing blocking), ``open`` (out of rounds),
    ``fix_failed`` (the fix broke verification and was dropped) or ``no_review``.
    """
    last = rounds[-1] if rounds else {}
    open_blocking = [] if last.get("status") in {"pass", "no_review", "out_of_time"} else [
        item for item in last.get("findings") or [] if is_blocking(item)
    ]
    return {
        "model": model or "",
        "reviews": len([r for r in rounds if r.get("status") != "no_review"]),
        "fixed": sum(len([f for f in r.get("findings") or [] if is_blocking(f)]) for r in rounds if r.get("status") == "fixed"),
        "open_blocking": open_blocking,
        "summary": str(last.get("summary") or ""),
        "status": str(last.get("status") or ""),
    }
