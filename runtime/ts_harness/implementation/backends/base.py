"""Coding backend protocol and request/result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class CodingRequest:
    repo_url: str
    base_branch: str
    work_order_id: str
    work_order_body: str
    workspace_dir: Path
    provider: str
    model: str | None = None
    clone_depth: int = 1
    refinery_artifacts: dict[str, Any] | None = None
    foundry_artifacts: dict[str, Any] | None = None
    planner_artifacts: dict[str, Any] | None = None
    approved_prd_markdown: str | None = None
    qa_feedback: str | None = None
    pr_url: str | None = None
    requested_by: str | None = None
    requested_by_github_login: str | None = None
    program_id: str | None = None
    delivery_workflow_id: str | None = None
    decision_id: str | None = None
    mode: str = "implement"
    review_feedback: str | None = None
    revision_files: list[str] | None = None
    human_change_requests: list[str] | None = None
    knowledge_notes: list[dict[str, Any]] | None = None
    # Design references the work order names: [{id, title, summary, url, mime, data_url}].
    design_refs: list[dict[str, Any]] | None = None
    # Pre-push review: the reviewer's model (None skips it) and the local fix rounds it may ask for.
    review_model: str | None = None
    review_rounds: int = 0
    review_budget_seconds: int = 0


def attribution_markdown(request: CodingRequest) -> str:
    lines = []
    if request.requested_by:
        who = request.requested_by
        if request.requested_by_github_login:
            who = f"{who} (@{request.requested_by_github_login})"
        lines.append(f"- Requested by: {who}")
    if request.program_id:
        lines.append(f"- Program: `{request.program_id}`")
    if request.decision_id:
        lines.append(f"- Decision: `{request.decision_id}`")
    if not lines:
        return ""
    return "\n## Attribution\n\n" + "\n".join(lines) + "\n"


def commit_message(request: CodingRequest) -> str:
    message = f"Implement {request.work_order_id}"
    trailers = []
    if request.program_id:
        trailers.append(f"TS-Harness-Program: {request.program_id}")
    if request.requested_by:
        name = request.requested_by_github_login or request.requested_by.split("@", 1)[0]
        trailers.append(f"Co-authored-by: {name} <{request.requested_by}>")
    if trailers:
        message += "\n\n" + "\n".join(trailers)
    return message


@dataclass
class CodingResult:
    ok: bool
    branch: str
    summary: str
    changed_files: list[str] = field(default_factory=list)
    log_tail: str = ""
    log_path: str | None = None
    error: str | None = None
    pr_url: str | None = None
    head_sha: str | None = None
    refresh_method: str | None = None
    resolved_files: list[str] = field(default_factory=list)
    security: dict[str, Any] = field(default_factory=dict)
    # Token usage per OpenCode session and model, for a coding worker to report with its result.
    usage: list[dict[str, Any]] = field(default_factory=list)
    # Host verification of the final worktree (verification_evidence), for review and QA.
    verification: dict[str, Any] = field(default_factory=dict)
    # Screenshots published for review and QA (publish_screenshots result).
    screenshots: dict[str, Any] = field(default_factory=dict)
    # What the pre-push review found and fixed (pre_push_review.summary_record).
    pre_push_review: dict[str, Any] = field(default_factory=dict)
    # Seconds spent in each phase of the run: setup, coding, verification, review, push.
    timings: dict[str, float] = field(default_factory=dict)


def security_markdown(security: dict[str, Any]) -> str:
    """PR-body section for the host scan; file and line only, never values."""
    if not security:
        return ""
    lines: list[str] = []
    secrets = security.get("secret_scan")
    if secrets is not None:
        count = len(secrets.get("findings") or [])
        lines.append(f"- Secret scan ({secrets.get('tool')}): {'no findings' if not count else f'{count} finding(s)'}")
    audit = security.get("dependency_audit")
    if audit is not None:
        if audit.get("skipped_reason") == "no_manifest_changes":
            lines.append("- Dependency audit: no manifest changes")
        elif audit.get("skipped_reason"):
            lines.append(f"- Dependency audit: not run ({audit['skipped_reason']})")
        else:
            count = len(audit.get("findings") or [])
            preexisting = int(audit.get("preexisting") or 0)
            lines.append(
                f"- Dependency audit ({audit.get('tool')}, {audit.get('severity', 'high')}+): "
                + ("no new findings" if not count else f"{count} new finding(s)")
                + (f"; {preexisting} already on the base branch" if preexisting else "")
            )
    if not lines:
        return ""
    return "\n## Security scan\n\n" + "\n".join(lines) + "\n"


class CodingBackend(Protocol):
    def implement(self, request: CodingRequest) -> CodingResult:
        """Implement a work order in a host worktree and return evidence."""
