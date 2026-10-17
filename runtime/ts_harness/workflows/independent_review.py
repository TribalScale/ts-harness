"""Publish ``harness/independent-review``: a PR needs approval from someone other than the PRD approver.

Branch protection makes the status required, so GitHub itself blocks the merge
until TS Harness posts ``success``. The on/off switch is read from the program's
pinned ``governance_snapshot``, never from live settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ts_harness.config.governance import independent_review_enabled, program_governance_snapshot
from ts_harness.integrations.github_client import GitHubClient, parse_github_pr_url

logger = logging.getLogger(__name__)

STATUS_CONTEXT = "harness/independent-review"
PRD_GATE_STEPS = frozenset({"approve_prd"})


@dataclass(frozen=True)
class ReviewVerdict:
    state: str
    description: str
    prd_approver: str | None = None
    reviewer: str | None = None


def _person(value: Any) -> str:
    return str(value or "").strip().lower()


def logins_to_emails(github_logins: dict[str, str] | None) -> dict[str, str]:
    """Invert ``identity.github_logins`` (email -> login) to lowercase login -> email."""
    return {_person(login): _person(email) for email, login in dict(github_logins or {}).items() if login and email}


def prd_approver_from_decisions(decisions: list[dict[str, Any]]) -> str | None:
    """Latest approved PRD gate; the approver is identified by email, else actor id."""
    approved = [
        d
        for d in decisions
        if d.get("kind") == "gate"
        and d.get("decision") == "approved"
        and str((d.get("details") or {}).get("step_name") or "") in PRD_GATE_STEPS
    ]
    if not approved:
        return None
    latest = sorted(approved, key=lambda d: str(d.get("created_at") or ""))[-1]
    return _person(latest.get("actor_email")) or _person(latest.get("actor_id")) or None


def latest_review_states(reviews: list[dict[str, Any]]) -> dict[str, str]:
    """GitHub keeps every review; only each reviewer's latest decisive one counts."""
    states: dict[str, str] = {}
    for review in sorted(reviews, key=lambda r: str(r.get("submitted_at") or "")):
        login = _person((review.get("user") or {}).get("login"))
        state = str(review.get("state") or "").upper()
        if login and state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            states[login] = state
    return states


def evaluate(
    *,
    enabled: bool,
    reviews: list[dict[str, Any]],
    prd_approver: str | None,
    login_emails: dict[str, str],
) -> ReviewVerdict:
    if not enabled:
        return ReviewVerdict("success", "disabled by tenant setting", prd_approver)
    approvers = [login for login, state in latest_review_states(reviews).items() if state == "APPROVED"]
    unmapped = [login for login in approvers if login not in login_emails]
    for login in approvers:
        email = login_emails.get(login)
        if email and email != prd_approver:
            return ReviewVerdict("success", f"approved by {login}", prd_approver, login)
    if any(login_emails.get(login) == prd_approver for login in approvers):
        return ReviewVerdict("pending", "PRD approver cannot be the only approving reviewer", prd_approver)
    if unmapped:
        return ReviewVerdict(
            "pending", f"reviewer not mapped in identity.github_logins: {', '.join(sorted(unmapped))}", prd_approver
        )
    return ReviewVerdict("pending", "waiting for an approving review from someone other than the PRD approver", prd_approver)


def _live_identity() -> Any:
    try:
        from ts_harness.gateway.server import get_runtime

        return getattr(get_runtime().config, "identity", None)
    except Exception:
        return None


def _default_memory() -> Any:
    from ts_harness.memory.client import get_memory_client

    return get_memory_client()


def owning_program(memory: Any, pr_url: str, *, tenant_id: str | None = None) -> dict[str, Any] | None:
    refs = list((memory.find_external_refs(kind="pull_request", ref=pr_url, tenant_id=tenant_id) or {}).get("refs") or [])
    return refs[-1] if refs else None


def verdict_for_pr(
    pr_url: str,
    *,
    memory: Any,
    github: GitHubClient,
    identity: Any = None,
    tenant_id: str | None = None,
) -> ReviewVerdict:
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        raise ValueError(f"not a GitHub pull request URL: {pr_url}")
    owner, repo, number = parsed
    ref = owning_program(memory, pr_url, tenant_id=tenant_id)
    program = {"metadata": (ref or {}).get("program_metadata") or {}}
    enabled = independent_review_enabled(program_governance_snapshot(program))
    prd_approver = None
    if ref:
        root = ref.get("root_program_id") or ref.get("program_id")
        decisions = list((memory.list_decisions(program_id=root, kind="gate") or {}).get("decisions") or [])
        prd_approver = prd_approver_from_decisions(decisions)
    identity = identity if identity is not None else _live_identity()
    login_emails = logins_to_emails(getattr(identity, "github_logins", None))
    reviews = github.list_reviews(owner, repo, number) if enabled else []
    return evaluate(enabled=enabled, reviews=reviews, prd_approver=prd_approver, login_emails=login_emails)


def publish(
    pr_url: str,
    *,
    head_sha: str | None = None,
    memory: Any = None,
    github: GitHubClient | None = None,
    identity: Any = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Compute the verdict for ``pr_url`` and post it on the PR head commit."""
    memory = memory or _default_memory()
    github = github or GitHubClient.from_env()
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        raise ValueError(f"not a GitHub pull request URL: {pr_url}")
    owner, repo, number = parsed
    sha = head_sha or str(((github.get_pull(owner, repo, number) or {}).get("head") or {}).get("sha") or "")
    if not sha:
        raise ValueError(f"cannot resolve head SHA for {pr_url}")
    verdict = verdict_for_pr(pr_url, memory=memory, github=github, identity=identity, tenant_id=tenant_id)
    github.create_status(owner, repo, sha, state=verdict.state, context=STATUS_CONTEXT, description=verdict.description)
    return {
        "pr_url": pr_url,
        "head_sha": sha,
        "state": verdict.state,
        "description": verdict.description,
        "prd_approver": verdict.prd_approver,
        "reviewer": verdict.reviewer,
    }


def on_github_event(event: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    """Webhook subscriber for PR opened/synchronized and review events."""
    pr_url = str(event.get("pr_url") or "")
    if not pr_url:
        return {"status": "ignored", "reason": "event without pr_url"}
    memory = _default_memory()
    if owning_program(memory, pr_url, tenant_id=tenant_id) is None:
        return {"status": "ignored", "reason": "PR is not owned by a TS Harness program"}
    return publish(pr_url, head_sha=str(event.get("head_sha") or "") or None, memory=memory, tenant_id=tenant_id)


def protection_warnings(github: GitHubClient, owner: str, repo: str, branch: str, *, enabled: bool) -> list[str]:
    """Warn when the base branch would let a PR merge without TS Harness's status."""
    if not enabled:
        return []
    try:
        contexts = github.required_status_contexts(owner, repo, branch)
    except Exception as exc:  # noqa: BLE001
        return [f"could not read branch protection for {owner}/{repo}@{branch}: {exc}"]
    if contexts is None:
        return [f"{owner}/{repo}@{branch} is not protected; {STATUS_CONTEXT} is not enforced"]
    if STATUS_CONTEXT not in contexts:
        return [f"{owner}/{repo}@{branch} branch protection does not require {STATUS_CONTEXT}"]
    return []
