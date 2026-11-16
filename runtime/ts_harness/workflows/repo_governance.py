"""Check, and optionally apply, what governed delivery needs on a repo's base branch.

GitHub enforces the merge rules TS Harness relies on: a PR, an approval, and passing
checks (the repo's CI plus ``harness/code-review``, ``harness/qa`` and
``harness/security-scan``). This reports what is missing, including CI that never
runs on PRs into the branch, and builds the protection payload ``--apply`` sends.
"""

from __future__ import annotations

import base64
import fnmatch
from dataclasses import dataclass, field
from typing import Any

HARNESS_REQUIRED = ("harness/code-review", "harness/qa", "harness/security-scan")
HARNESS_OPTIONAL = ("harness/independent-review",)


@dataclass
class GovernanceReport:
    repo: str
    branch: str
    protected: bool = False
    required_now: list[str] = field(default_factory=list)
    ci_checks: list[str] = field(default_factory=list)
    ci_on_pull_requests: list[str] = field(default_factory=list)
    approvals: int = 0
    dismiss_stale: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def wanted_checks(self) -> list[str]:
        return sorted({*self.ci_checks, *HARNESS_REQUIRED})

    def text(self) -> str:
        lines = [f"{self.repo} @ {self.branch}"]
        lines.append(f"  protected: {'yes' if self.protected else 'no'}")
        lines.append(f"  approvals required: {self.approvals}; dismiss stale approvals: {self.dismiss_stale}")
        lines.append(f"  CI workflows on PRs into {self.branch}: {', '.join(self.ci_on_pull_requests) or 'none'}")
        lines.append(f"  CI checks seen: {', '.join(self.ci_checks) or 'none'}")
        lines.append(f"  required now: {', '.join(self.required_now) or 'none'}")
        lines.append(f"  required wanted: {', '.join(self.wanted_checks)}")
        lines += [f"  PROBLEM: {problem}" for problem in self.problems] or ["  ok"]
        return "\n".join(lines)


def workflow_runs_on_pr(workflow: dict[str, Any], branch: str) -> bool:
    """Whether a GitHub Actions workflow triggers on pull requests into ``branch``."""
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on:` as True
    if isinstance(on, str):
        return on in {"pull_request", "pull_request_target"}
    if isinstance(on, list):
        return any(item in {"pull_request", "pull_request_target"} for item in on)
    if not isinstance(on, dict):
        return False
    for event in ("pull_request", "pull_request_target"):
        if event not in on:
            continue
        spec = on[event] or {}
        branches = spec.get("branches") if isinstance(spec, dict) else None
        ignored = spec.get("branches-ignore") if isinstance(spec, dict) else None
        if branches is not None and not any(fnmatch.fnmatch(branch, str(p)) for p in branches):
            continue
        if ignored is not None and any(fnmatch.fnmatch(branch, str(p)) for p in ignored):
            continue
        return True
    return False


def _workflows(github: Any, owner: str, repo: str, branch: str) -> dict[str, dict[str, Any]]:
    import yaml

    tree = github.get_tree(owner, repo, branch) or {}
    found = {}
    for item in tree.get("tree") or []:
        path = str(item.get("path") or "")
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            payload = github.get_file(owner, repo, path, ref=branch) or {}
            try:
                found[path] = yaml.safe_load(base64.b64decode(str(payload.get("content") or "")).decode()) or {}
            except Exception:  # noqa: BLE001 - an unparsable workflow is reported as not running
                found[path] = {}
    return found


def check_repo(github: Any, owner: str, repo: str, branch: str) -> GovernanceReport:
    report = GovernanceReport(repo=f"{owner}/{repo}", branch=branch)
    protection = github.branch_protection(owner, repo, branch)
    report.protected = protection is not None
    if protection:
        checks = protection.get("required_status_checks") or {}
        report.required_now = sorted(
            {*(checks.get("contexts") or []), *(str(c.get("context")) for c in checks.get("checks") or [] if c.get("context"))}
        )
        reviews = protection.get("required_pull_request_reviews") or {}
        report.approvals = int(reviews.get("required_approving_review_count") or 0)
        report.dismiss_stale = bool(reviews.get("dismiss_stale_reviews"))
    workflows = _workflows(github, owner, repo, branch)
    on_pr = {path: data for path, data in workflows.items() if workflow_runs_on_pr(data, branch)}
    report.ci_on_pull_requests = sorted(on_pr)
    for data in on_pr.values():
        for job_id, job in (data.get("jobs") or {}).items():
            name = str((job or {}).get("name") or job_id) if isinstance(job, dict) else str(job_id)
            if "${{" not in name and not (isinstance(job, dict) and job.get("strategy")):
                report.ci_checks.append(name)
    report.ci_checks = sorted(set(report.ci_checks))
    if not report.ci_on_pull_requests:
        report.problems.append(f"no CI workflow runs on pull requests into {branch}; PRs merge on harness checks alone")
    if not report.protected:
        report.problems.append("branch is not protected: anyone with write access can merge without checks")
    missing = [name for name in report.wanted_checks if name not in report.required_now]
    if report.protected and missing:
        report.problems.append("required checks missing: " + ", ".join(missing))
    if report.approvals < 1:
        report.problems.append("no approving review required")
    elif not report.dismiss_stale:
        report.problems.append("stale approvals are not dismissed when new commits arrive")
    return report


def protection_body(report: GovernanceReport, current: dict[str, Any] | None) -> dict[str, Any]:
    """Protection that adds what governed delivery needs and keeps anything stricter already set."""
    current = current or {}
    reviews = current.get("required_pull_request_reviews") or {}
    checks = current.get("required_status_checks") or {}
    contexts = sorted({*report.required_now, *report.wanted_checks})
    return {
        "required_status_checks": {"strict": bool(checks.get("strict", True)), "contexts": contexts},
        "enforce_admins": bool((current.get("enforce_admins") or {}).get("enabled", False)),
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": bool(reviews.get("require_code_owner_reviews", False)),
            "required_approving_review_count": max(1, int(reviews.get("required_approving_review_count") or 0)),
        },
        "restrictions": None,
        "required_conversation_resolution": bool((current.get("required_conversation_resolution") or {}).get("enabled", False)),
        "allow_force_pushes": False,
        "allow_deletions": False,
    }
