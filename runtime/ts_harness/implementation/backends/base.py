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
            lines.append(
                f"- Dependency audit ({audit.get('tool')}, {audit.get('severity', 'high')}+): "
                + ("no findings" if not count else f"{count} finding(s)")
            )
    if not lines:
        return ""
    return "\n## Security scan\n\n" + "\n".join(lines) + "\n"


class CodingBackend(Protocol):
    def implement(self, request: CodingRequest) -> CodingResult:
        """Implement a work order in a host worktree and return evidence."""
