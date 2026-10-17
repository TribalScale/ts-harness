"""``ensure_fresh`` system step: block PRs whose base branch moved since QA."""

from __future__ import annotations

from typing import Any

from ts_harness.integrations.github_client import GitHubClient, parse_github_pr_url
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.merge_readiness import _resolve_pr_urls
from ts_harness.workflows.system_steps import StepBlocked


def pr_freshness(github: GitHubClient, pr_url: str) -> dict[str, Any]:
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        raise ValueError(f"ensure_fresh cannot parse PR URL: {pr_url}")
    owner, repo, number = parsed
    pull = github.get_pull(owner, repo, number)
    base_ref = str((pull.get("base") or {}).get("ref") or "")
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    entry: dict[str, Any] = {"pr_url": pr_url, "base_ref": base_ref, "head_sha": head_sha}
    if pull.get("merged") or pull.get("state") == "closed":
        return {**entry, "status": "skipped", "behind_by": 0}
    comparison = github.compare(owner, repo, base_ref, head_sha)
    behind_by = int(comparison.get("behind_by") or 0)
    return {**entry, "status": "stale" if behind_by > 0 else "fresh", "behind_by": behind_by}


def ensure_fresh(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
) -> dict[str, Any]:
    """Succeed when every PR contains its base head; otherwise block with the stale list."""
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    pr_urls = _resolve_pr_urls(
        {key: config[key] for key in ("pr_url", "pr_urls") if key in config},
        tasks=tasks,
        workflow=workflow,
    )
    if not pr_urls:
        raise ValueError("ensure_fresh resolved no PR URLs")
    github = github or GitHubClient.from_env()
    prs = [pr_freshness(github, url) for url in pr_urls]
    stale = [pr for pr in prs if pr["status"] == "stale"]
    if stale:
        detail = ", ".join(f"{pr['pr_url']} behind_by={pr['behind_by']}" for pr in stale)
        raise StepBlocked(f"stale_base: {detail}", output={"stale": stale, "prs": prs})
    return {"prs": prs, "fresh": True}
