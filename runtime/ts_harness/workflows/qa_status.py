"""QA results on the PR: the ``harness/qa`` commit status, and a comment with the verdict.

The status is pending while QA runs, then the verdict on each PR head. When QA finishes, each
PR also gets a comment with the summary, defects, acceptance criteria and evidence, so a
failed QA can be read on the PR and not only in the console.
"""

from __future__ import annotations

from typing import Any

from ts_harness.integrations.github_client import GitHubClient, parse_github_pr_url
from ts_harness.tools.approvals import AWAITING_TOOL_APPROVAL_PREFIX

STATUS_CONTEXT = "harness/qa"


def _prs(task_input: dict[str, Any]) -> list[tuple[str, str]]:
    prs = []
    for pr in task_input.get("prs") or []:
        if isinstance(pr, dict) and pr.get("pr_url") and pr.get("head_sha"):
            prs.append((str(pr["pr_url"]), str(pr["head_sha"])))
    return prs


def _publish(
    task_input: dict[str, Any],
    state: str,
    describe: Any,
    *,
    github: GitHubClient | None = None,
) -> list[str]:
    prs = _prs(task_input)
    if not prs:
        return []
    try:
        client = github or GitHubClient.from_env()
    except Exception as exc:  # noqa: BLE001 - posting is best effort
        return [f"qa_status_not_posted: {exc}"]
    warnings: list[str] = []
    for pr_url, head_sha in prs:
        parsed = parse_github_pr_url(pr_url)
        if parsed is None:
            warnings.append(f"qa_status_not_posted:{pr_url}: not a GitHub PR URL")
            continue
        description = describe(pr_url) if callable(describe) else describe
        pr_state = state(pr_url) if callable(state) else state
        try:
            client.create_status(*parsed[:2], head_sha, state=pr_state, context=STATUS_CONTEXT, description=description[:140])
        except Exception as exc:  # noqa: BLE001 - posting is best effort
            warnings.append(f"qa_status_post_failed:{pr_url}: {exc}")
    return warnings


def qa_started(task_input: dict[str, Any], *, github: GitHubClient | None = None) -> list[str]:
    return _publish(task_input, "pending", "QA running", github=github)


def _verdicts(output: Any) -> dict[str, dict[str, Any]]:
    output = output if isinstance(output, dict) else {}
    envelope = output.get("result") if isinstance(output.get("result"), dict) else output
    return {
        str(pr.get("pr_url")): pr
        for pr in envelope.get("prs") or []
        if isinstance(pr, dict) and pr.get("pr_url")
    }


def _defects(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in verdict.get("defects") or [] if isinstance(d, dict)]


def _failure_text(verdict: dict[str, Any]) -> str:
    defects = _defects(verdict)
    rated = [d for d in defects if d.get("severity")]
    if not rated:
        # QA verdicts often leave severity out; every defect on a failed verdict blocks it.
        return f"QA failed: {len(defects)} defect{'' if len(defects) == 1 else 's'}"
    blocking = sum(1 for d in rated if str(d.get("severity") or "").lower() == "blocking")
    return f"QA failed: {blocking} blocking of {len(defects)} defects"


COMMENT_MARKER = "<!-- ts-harness:qa -->"
_STATUS_ICON = {"passed": "✅", "met": "✅", "pass": "✅", "failed": "❌", "not_met": "❌", "unverified": "⚠️"}


def _cell(text: Any) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def qa_comment_body(verdict: dict[str, Any], *, head_sha: str, passed: bool) -> str:
    """Markdown comment with one PR's QA verdict."""
    lines = [COMMENT_MARKER, f"## QA (TS Harness): {'passed ✅' if passed else 'failed ❌'}", ""]
    if verdict.get("summary"):
        lines += [str(verdict["summary"]).strip(), ""]
    defects = _defects(verdict)
    if defects:
        lines += [f"### Defects ({len(defects)})", ""]
        for number, defect in enumerate(defects, 1):
            head = defect.get("criterion") or defect.get("title") or "Defect"
            severity = f" ({defect['severity']})" if defect.get("severity") else ""
            lines.append(f"{number}. **{_cell(head)}**{severity}")
            if defect.get("description"):
                lines.append(f"   {_cell(defect['description'])}")
            files = [str(f) for f in defect.get("files") or [] if f]
            if files:
                lines.append("   Files: " + ", ".join(f"`{f}`" for f in files[:8]))
            change = defect.get("requested_change") or defect.get("expected")
            if change:
                lines.append(f"   Requested change: {_cell(change)}")
        lines.append("")
    criteria = [c for c in verdict.get("acceptance_criteria") or [] if isinstance(c, dict)]
    if criteria:
        lines += ["### Acceptance criteria", "", "| | Criterion | Note |", "|---|---|---|"]
        for item in criteria:
            state = str(item.get("status") or "").lower()
            lines.append(f"| {_STATUS_ICON.get(state, state or '?')} | {_cell(item.get('criterion'))} | {_cell(item.get('note'))} |")
        lines.append("")
    evidence = [str(e) for e in verdict.get("tests") or [] if e]
    if evidence:
        lines += ["<details><summary>Evidence QA used</summary>", "", *(f"- {_cell(e)}" for e in evidence), "", "</details>", ""]
    lines.append(
        f"_On commit `{head_sha[:12]}`. "
        + ("A human still reviews and merges._" if passed else "The harness sends these defects back to the coding agent._")
    )
    return "\n".join(lines)


def _comment(
    task_input: dict[str, Any],
    verdicts: dict[str, dict[str, Any]],
    passed: Any,
    *,
    github: GitHubClient | None,
) -> list[str]:
    """One new comment per PR per QA run, so a failed run stays on the PR after a later pass."""
    warnings: list[str] = []
    for pr_url, head_sha in _prs(task_input):
        verdict = verdicts.get(pr_url)
        parsed = parse_github_pr_url(pr_url)
        if not verdict or parsed is None:
            continue
        try:
            client = github or GitHubClient.from_env()
            body = qa_comment_body(verdict, head_sha=head_sha, passed=passed(pr_url) if callable(passed) else bool(passed))
            client.comment(*parsed, body)
        except Exception as exc:  # noqa: BLE001 - posting is best effort
            warnings.append(f"qa_comment_failed:{pr_url}: {exc}")
    return warnings


def qa_finished(task_input: dict[str, Any], result: dict[str, Any], *, github: GitHubClient | None = None) -> list[str]:
    status = str(result.get("status") or "")
    error = str(result.get("error") or "")
    if error.startswith(AWAITING_TOOL_APPROVAL_PREFIX):
        return []
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    verdicts = _verdicts(output)
    # A verdict carried forward from an earlier run was already commented on.
    fresh = verdicts if not output.get("carried_forward") else {}
    if status == "succeeded":
        return _publish(task_input, "success", "QA passed", github=github) + _comment(
            task_input, fresh, True, github=github
        )
    if status == "blocked" and verdicts:

        def passed(pr_url: str) -> bool:
            return str((verdicts.get(pr_url) or {}).get("status")) == "success"

        def state(pr_url: str) -> str:
            return "success" if passed(pr_url) else "failure"

        def describe(pr_url: str) -> str:
            verdict = verdicts.get(pr_url) or {}
            return "QA passed" if passed(pr_url) else _failure_text(verdict)

        return _publish(task_input, state, describe, github=github) + _comment(task_input, fresh, passed, github=github)
    return _publish(task_input, "error", f"QA could not finish: {error or status}", github=github)


def qa_override(task_input: dict[str, Any], actor: str, *, github: GitHubClient | None = None) -> list[str]:
    return _publish(task_input, "success", f"accepted by {actor} (human override)", github=github)
