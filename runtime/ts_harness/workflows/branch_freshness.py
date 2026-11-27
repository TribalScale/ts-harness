"""``ensure_fresh`` system step: before the merge wait, send back PRs that conflict with their base.

A PR that is behind its base but merges cleanly goes on to the merge wait as it is (the default
``merge_readiness.refresh: on_conflict``); ``always`` sends it back to be updated first.
"""

from __future__ import annotations

from typing import Any

from ts_harness.integrations.github_client import GitHubClient
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.merge_modes import detect_for_pr
from ts_harness.workflows.merge_readiness import (
    _live_program_defaults,
    _resolve_pr_urls,
    needs_refresh,
    pr_base_state,
    refresh_policy,
)
from ts_harness.workflows.system_steps import StepBlocked

# GitHub computes mergeability after the base moves: wait up to about 6 seconds for it.
MERGEABLE_ATTEMPTS = 4
MERGEABLE_DELAY = 2.0


def pr_freshness(github: GitHubClient, pr_url: str, **kwargs: Any) -> dict[str, Any]:
    return pr_base_state(github, pr_url, **kwargs)


def ensure_fresh(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
    program_defaults: dict[str, Any] | None = None,
    sleep: Any = None,
) -> dict[str, Any]:
    """Succeed unless a PR needs updating first; otherwise block with the list (``stale``)."""
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    pr_urls = _resolve_pr_urls(
        {key: config[key] for key in ("pr_url", "pr_urls") if key in config},
        tasks=tasks,
        workflow=workflow,
    )
    if not pr_urls:
        raise ValueError("ensure_fresh resolved no PR URLs")
    github = github or GitHubClient.from_env()
    policy = refresh_policy(program_defaults if program_defaults is not None else _live_program_defaults())
    timing: dict[str, Any] = {"attempts": MERGEABLE_ATTEMPTS, "delay": MERGEABLE_DELAY}
    if sleep is not None:
        timing["sleep"] = sleep
    defaults = program_defaults if program_defaults is not None else _live_program_defaults()
    prs = [pr_base_state(github, url, **timing) for url in pr_urls]
    for pr in prs:
        if pr["status"] != "skipped":
            pr["merge_mode"] = detect_for_pr(github, pr["pr_url"], pr["base_ref"], program_defaults=defaults)
    stale = [pr for pr in prs if needs_refresh(pr, policy)]
    if stale:
        detail = ", ".join(f"{pr['pr_url']} behind_by={pr['behind_by']} {pr['status']}" for pr in stale)
        raise StepBlocked(f"stale_base: {detail}", output={"stale": stale, "prs": prs, "refresh_policy": policy})
    return {
        "prs": prs,
        "fresh": all(pr["status"] in {"fresh", "skipped"} for pr in prs),
        "behind_clean": [pr["pr_url"] for pr in prs if pr["status"] in {"behind", "behind_unknown"}],
        "refresh_policy": policy,
    }
