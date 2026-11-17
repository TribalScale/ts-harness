"""``harness/qa`` commit status: pending while QA runs, then the QA verdict on each PR head."""

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


def _failure_text(verdict: dict[str, Any]) -> str:
    defects = [d for d in verdict.get("defects") or [] if isinstance(d, dict)]
    blocking = sum(1 for d in defects if str(d.get("severity") or "").lower() == "blocking")
    return f"QA failed: {blocking} blocking of {len(defects)} defects"


def qa_finished(task_input: dict[str, Any], result: dict[str, Any], *, github: GitHubClient | None = None) -> list[str]:
    status = str(result.get("status") or "")
    error = str(result.get("error") or "")
    if error.startswith(AWAITING_TOOL_APPROVAL_PREFIX):
        return []
    verdicts = _verdicts(result.get("output"))
    if status == "succeeded":
        return _publish(task_input, "success", "QA passed", github=github)
    if status == "blocked" and verdicts:

        def state(pr_url: str) -> str:
            return "success" if str((verdicts.get(pr_url) or {}).get("status")) == "success" else "failure"

        def describe(pr_url: str) -> str:
            verdict = verdicts.get(pr_url) or {}
            return "QA passed" if verdict.get("status") == "success" else _failure_text(verdict)

        return _publish(task_input, state, describe, github=github)
    return _publish(task_input, "error", f"QA could not finish: {error or status}", github=github)


def qa_override(task_input: dict[str, Any], actor: str, *, github: GitHubClient | None = None) -> list[str]:
    return _publish(task_input, "success", f"accepted by {actor} (human override)", github=github)
