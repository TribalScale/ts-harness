"""External checks gate the merge: prepare ``github.pr_checks`` waits and record merges.

A checks wait is pinned to PR head SHAs, so a revision push (new SHA) needs a
new wait. Required checks come from ``merge_readiness.required_checks`` and
``merge_readiness.policy.categories`` per repo; without either, every check
reported on the head SHA must pass. With a ``policy``, a required check that
never reports fails once the other required checks finish and the grace
period has passed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ts_harness.config.governance import independent_review_enabled, program_governance_snapshot
from ts_harness.integrations.github_client import GitHubClient, parse_github_pr_url, parse_repo_slug
from ts_harness.workflows import independent_review, merge_policy
from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload

logger = logging.getLogger(__name__)

CHECKS_EVENT = "github.pr_checks"
TERMINAL_STATES = frozenset({"success", "failure"})


class MergePolicyBlocked(ValueError):
    """``enforce`` policy refuses to wait: categories unmapped or branch unprotected."""


def required_checks_for(repo: str, program_defaults: dict[str, Any] | None) -> list[str]:
    return merge_policy.resolve(repo, program_defaults).required_names


def _live_program_defaults() -> dict[str, Any]:
    try:
        from ts_harness.gateway.server import get_runtime

        return dict(getattr(get_runtime().config, "program_defaults", None) or {})
    except Exception:
        return {}


def _resolve_pr_urls(spec: dict[str, Any], *, tasks: list[dict[str, Any]], workflow: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("pr_url", "pr_urls"):
        if key not in spec:
            continue
        resolved = resolve_value(spec[key], tasks=tasks, workflow=workflow)
        values = resolved if isinstance(resolved, (list, tuple)) else [resolved]
        for value in values:
            if isinstance(value, dict):
                value = value.get("pr_url")
            text = str(value or "").strip()
            if text and text not in urls:
                urls.append(text)
    return urls


def _apply_missing_grace(head: dict[str, Any], summary: dict[str, Any], now: float) -> dict[str, Any]:
    policy = head.get("policy") or {}
    missing = list(summary.get("missing") or [])
    if not policy or summary.get("state") != "pending" or not missing:
        return summary
    if sorted(summary.get("pending") or []) != sorted(missing):
        return summary
    pinned_at = float(head["pinned_at"]) if head.get("pinned_at") is not None else now
    if now - pinned_at < float(policy.get("grace_seconds") or 0):
        return summary
    already_failed = [name for name in summary.get("failed") or [] if not str(name).startswith("required_check_missing:")]
    failed = sorted({*already_failed, *(f"required_check_missing:{name}" for name in missing)})
    return {**summary, "state": "failure", "failed": failed}


def aggregate_checks(
    github: GitHubClient, heads: list[dict[str, Any]], *, now: float | None = None
) -> dict[str, Any]:
    """Summarize checks for every pinned PR head; any failure fails, any pending waits."""
    now = time.time() if now is None else now
    prs: list[dict[str, Any]] = []
    for head in heads:
        slug = parse_repo_slug(str(head.get("repo") or ""))
        if slug is None:
            raise ValueError(f"invalid repo on pinned head: {head.get('repo')!r}")
        summary = github.check_summary(slug[0], slug[1], str(head["head_sha"]), required=head.get("required_checks") or None)
        # One red check must not finish the wait while another required check has not reported.
        if summary.get("state") == "failure" and (summary.get("pending") or summary.get("missing")):
            summary = {**summary, "state": "pending"}
        summary = _apply_missing_grace(head, summary, now)
        entry = {**head, "state": summary["state"], "checks": summary["checks"], "failed": summary["failed"]}
        categories = (head.get("policy") or {}).get("categories")
        if categories:
            entry["category_results"] = merge_policy.category_results(categories, summary["checks"] or {})
        prs.append(entry)
    states = {pr["state"] for pr in prs}
    if "failure" in states:
        state = "failure"
    elif "pending" in states or not prs:
        state = "pending"
    else:
        state = "success"
    return {"state": state, "prs": prs}


def checks_payload(aggregate: dict[str, Any], *, warnings: list[str] | None = None) -> dict[str, Any]:
    prs = list(aggregate.get("prs") or [])
    first = prs[0] if prs else {}
    payload = {
        "head_sha": first.get("head_sha") or "",
        "repo": first.get("repo") or "",
        "checks_state": aggregate.get("state"),
        "checks": (first.get("checks") or {}) if len(prs) == 1 else {},
        "failed_checks": sorted(
            {f"{pr.get('pr_url') or pr.get('repo')}:{name}" for pr in prs for name in pr.get("failed") or []}
        ),
        "prs": [
            {
                "pr_url": pr["pr_url"],
                "head_sha": pr["head_sha"],
                "state": pr["state"],
                "failed": pr.get("failed") or [],
                **({"policy": pr["policy"]} if pr.get("policy") else {}),
                **({"category_results": pr["category_results"]} if pr.get("category_results") else {}),
            }
            for pr in prs
        ],
    }
    if warnings:
        payload["policy_warnings"] = list(warnings)
    return payload


def workflow_program(memory: Any, workflow: dict[str, Any]) -> str | None:
    """The workflow's program, or its parent workflow's (wave children have none of their own)."""
    program_id = str(workflow.get("program_id") or "").strip()
    parent_id = str(workflow.get("parent_workflow_id") or "").strip()
    if program_id or not parent_id:
        return program_id or None
    try:
        parent, _ = load_workflow_payload(memory, parent_id)
    except Exception:  # noqa: BLE001
        return None
    return str(parent.get("program_id") or "").strip() or None


def pinned_heads(wait: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(head) for head in (wait.get("options") or {}).get("heads") or [] if isinstance(head, dict)]


def _pin_head(
    pr_url: str,
    *,
    github: GitHubClient,
    memory: Any,
    tenant_id: str | None,
    defaults: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        raise ValueError(f"await_pr_checks needs a GitHub PR URL, got {pr_url!r}")
    owner, repo, number = parsed
    pull = github.get_pull(owner, repo, number)
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    if not head_sha:
        raise ValueError(f"cannot resolve head SHA for {pr_url}")
    base_branch = str((pull.get("base") or {}).get("ref") or "")
    full_name = str(((pull.get("base") or {}).get("repo") or {}).get("full_name") or f"{owner}/{repo}")

    ref = independent_review.owning_program(memory, pr_url, tenant_id=tenant_id)
    enabled = independent_review_enabled(
        program_governance_snapshot({"metadata": (ref or {}).get("program_metadata") or {}})
    )
    try:
        independent_review.publish(pr_url, head_sha=head_sha, memory=memory, github=github, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not publish {independent_review.STATUS_CONTEXT} on {pr_url}: {exc}")
    for warning in independent_review.protection_warnings(github, owner, repo, base_branch, enabled=enabled):
        if warning not in warnings:
            warnings.append(warning)
    policy = merge_policy.resolve(full_name, defaults)
    _check_policy(policy, github=github, repo=full_name, base_branch=base_branch, warnings=warnings)
    head = {
        "pr_url": pr_url,
        "head_sha": head_sha,
        "repo": full_name,
        "base_branch": base_branch,
        "required_checks": _required_check_names(
            policy, github=github, owner=owner, repo=repo, base_branch=base_branch
        ),
        "program_id": (ref or {}).get("program_id"),
    }
    if policy.configured:
        head["policy"] = policy.pinned()
        head["pinned_at"] = int(time.time())
    return head


SECURITY_STATUS_CONTEXT = "harness/security-scan"
REQUIRED_CLAW_CONTEXTS = ("harness/code-review", "harness/security-scan")
SCAN_MISMATCH_DESCRIPTION = "security scan does not cover this commit"


def _required_check_names(
    policy: merge_policy.RepoPolicy,
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    base_branch: str,
) -> list[str]:
    """Policy names win. With none configured, use branch protection minus TS Harness's own contexts."""
    if policy.required_names:
        return list(policy.required_names)
    if not base_branch:
        return []
    try:
        contexts = github.required_status_contexts(owner, repo, base_branch)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(contexts, list):
        return []
    return [name for name in contexts if name not in merge_policy.CLAW_CONTEXTS]


def security_status(security: dict[str, Any]) -> tuple[str, str]:
    """(state, description) for ``harness/security-scan`` from a delivery's security block."""
    secrets = len(((security.get("secret_scan") or {}).get("findings")) or [])
    audit = security.get("dependency_audit") or {}
    vulns = len(audit.get("findings") or [])
    if secrets or vulns:
        return "failure", f"{secrets} secret finding(s), {vulns} vulnerable dependency finding(s)"
    if audit.get("skipped_reason") == "dependency_audit_unavailable":
        return "failure", "dependency audit unavailable"
    return "success", "No secrets or vulnerable dependencies found"


def security_status_for_head(security: dict[str, Any], head_sha: str) -> tuple[str, str]:
    """A success recorded on an earlier commit must not be copied onto a later head."""
    scanned = str(security.get("scanned_sha") or "")
    if scanned and scanned != str(head_sha or ""):
        return "failure", SCAN_MISMATCH_DESCRIPTION
    return security_status(security)


def _publish_security_status(
    github: GitHubClient, heads: list[dict[str, Any]], security: dict[str, Any], warnings: list[str]
) -> None:
    for head in heads:
        state, description = security_status_for_head(security, str(head.get("head_sha") or ""))
        slug = parse_repo_slug(str(head.get("repo") or ""))
        if slug is None:
            continue
        try:
            github.create_status(
                slug[0], slug[1], str(head["head_sha"]), state=state, context=SECURITY_STATUS_CONTEXT, description=description
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"could not publish {SECURITY_STATUS_CONTEXT} on {head.get('pr_url')}: {exc}")


def _check_policy(
    policy: merge_policy.RepoPolicy,
    *,
    github: GitHubClient,
    repo: str,
    base_branch: str,
    warnings: list[str],
) -> None:
    """Record policy gaps as warnings; raise ``MergePolicyBlocked`` in enforce mode."""
    if not policy.configured:
        return
    uncovered = f"policy_incomplete:{repo}: {', '.join(policy.uncovered)}" if policy.uncovered else ""
    missing = _unprotected_checks(policy, github=github, repo=repo, base_branch=base_branch)
    unprotected = (
        f"{repo}@{base_branch} branch protection does not require: {', '.join(missing)}" if missing else ""
    )
    for problem in (uncovered, unprotected):
        if problem and problem not in warnings:
            warnings.append(problem)
    if not policy.enforce:
        return
    blocking = [uncovered] + ([unprotected] if policy.require_branch_protection else [])
    blocking = [problem for problem in blocking if problem]
    if blocking:
        raise MergePolicyBlocked("; ".join(blocking))


def _unprotected_checks(
    policy: merge_policy.RepoPolicy, *, github: GitHubClient, repo: str, base_branch: str
) -> list[str]:
    names = [name for name in policy.required_names if name not in merge_policy.CLAW_CONTEXTS]
    for context in REQUIRED_CLAW_CONTEXTS:
        if context not in names:
            names.append(context)
    if not names or not base_branch:
        return []
    slug = parse_repo_slug(repo)
    if slug is None:
        return []
    try:
        contexts = github.required_status_contexts(slug[0], slug[1], base_branch)
    except Exception:  # noqa: BLE001
        return []
    required = set(contexts or [])
    return [name for name in names if name not in required]


def prepare_checks_wait(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
    program_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pin the wait to PR head SHAs and return ``ready`` when checks already finished."""
    workflow_id = str(task.get("workflow_id") or "")
    tenant_id = task.get("tenant_id")
    workflow, tasks = load_workflow_payload(memory, workflow_id)
    pr_urls = _resolve_pr_urls(dict(config.get("pr_checks") or {}), tasks=tasks, workflow=workflow)
    if not pr_urls:
        raise ValueError("await_pr_checks resolved no PR URLs")
    github = github or GitHubClient.from_env()
    defaults = program_defaults if program_defaults is not None else _live_program_defaults()
    warnings: list[str] = []
    heads = [
        _pin_head(url, github=github, memory=memory, tenant_id=tenant_id, defaults=defaults, warnings=warnings)
        for url in pr_urls
    ]

    security_spec = dict(config.get("pr_checks") or {}).get("security")
    if security_spec:
        security = resolve_value(security_spec, tasks=tasks, workflow=workflow)
        if isinstance(security, dict) and security:
            _publish_security_status(github, heads, security, warnings)

    match = {"head_sha": heads[0]["head_sha"], "repo": heads[0]["repo"]} if len(heads) == 1 else {}
    options = {
        "heads": [{k: v for k, v in head.items() if k != "program_id"} for head in heads],
        "warnings": warnings,
    }
    memory.patch_task_input(
        str(task["id"]),
        {**(task.get("input") or {}), "_wait_match": match, "_wait_options": options},
        actor="workflow-runner",
    )
    workflow_program_id = workflow_program(memory, workflow)
    for head in heads:
        program_id = head.get("program_id") or workflow_program_id
        if not program_id:
            continue
        try:
            memory.record_program_external_ref(
                str(program_id),
                kind="pull_request",
                ref=head["pr_url"],
                state="open",
                workflow_id=workflow_id or None,
                metadata={"head_sha": head["head_sha"], "merge_readiness_warnings": warnings},
                actor="workflow-runner",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("merge readiness warnings not recorded for %s: %s", head["pr_url"], exc)

    aggregate = aggregate_checks(github, options["heads"])
    result: dict[str, Any] = {"match": match, "options": options, "summary": aggregate}
    if aggregate["state"] in TERMINAL_STATES:
        result["ready"] = checks_payload(aggregate, warnings=warnings)
    return result


BASE_ADVANCED_REASON = "external_blocked:github.merge base_advanced"


def _wait_pr_urls(wait: dict[str, Any]) -> list[str]:
    urls = [str(url) for url in wait.get("match_pr_urls") or [] if url]
    if not urls and wait.get("match_pr_url"):
        urls = [str(wait["match_pr_url"])]
    return urls


def fan_out_base_advanced(
    event: dict[str, Any],
    *,
    tenant_id: str,
    memory: Any = None,
    github: GitHubClient | None = None,
    engine: Any = None,
) -> list[dict[str, Any]]:
    """Block sibling merge waits whose PRs share the merged PR's repo and base branch."""
    merged_url = str(event.get("pr_url") or "")
    parsed = parse_github_pr_url(merged_url)
    if parsed is None:
        return []
    owner, repo, number = parsed
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    waits = list((memory.find_external_waits(event_type="github.merge", tenant_id=tenant_id) or {}).get("waits") or [])
    candidates = []
    for wait in waits:
        urls = _wait_pr_urls(wait)
        if not urls or merged_url in urls:
            continue
        same_repo = [url for url in urls if (parse_github_pr_url(url) or ("", "", 0))[:2] == (owner, repo)]
        if same_repo:
            candidates.append((wait, same_repo))
    if not candidates:
        return []
    github = github or GitHubClient.from_env()
    base_ref = str(event.get("base_branch") or event.get("base_ref") or "")
    if not base_ref:
        base_ref = str((github.get_pull(owner, repo, number).get("base") or {}).get("ref") or "")
    if engine is None:
        from ts_harness.workflows.engine import get_workflow_engine

        engine = get_workflow_engine()
    blocked: list[dict[str, Any]] = []
    for wait, urls in candidates:
        stale = []
        for url in urls:
            sibling_owner, sibling_repo, sibling_number = parse_github_pr_url(url)
            pull = github.get_pull(sibling_owner, sibling_repo, sibling_number)
            if pull.get("merged") or pull.get("state") == "closed":
                continue
            if str((pull.get("base") or {}).get("ref") or "") != base_ref:
                continue
            head_sha = str((pull.get("head") or {}).get("sha") or "")
            behind_by = int((github.compare(sibling_owner, sibling_repo, base_ref, head_sha) or {}).get("behind_by") or 0)
            if behind_by > 0:
                stale.append({"pr_url": url, "base_ref": base_ref, "head_sha": head_sha, "behind_by": behind_by})
        if not stale:
            continue
        workflow_id, task_id = str(wait.get("workflow_id") or ""), str(wait.get("task_id") or "")
        try:
            engine.block_wait(
                workflow_id,
                task_id,
                BASE_ADVANCED_REASON,
                output={"stale": stale, "merged_pr_url": merged_url},
            )
        except Exception as exc:  # noqa: BLE001 - one bad sibling must not stop the others
            logger.warning("base_advanced fan-out failed workflow_id=%s task_id=%s error=%s", workflow_id, task_id, exc)
            continue
        blocked.append({"workflow_id": workflow_id, "task_id": task_id, "stale": stale})
    return blocked


def on_merge(event: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    """Webhook subscriber: record who merged the PR and refresh sibling PRs on the same base."""
    from ts_harness.memory.client import get_memory_client

    pr_url = str(event.get("pr_url") or "")
    memory = get_memory_client()
    result: dict[str, Any] = {"status": "ignored", "reason": "PR is not owned by a TS Harness program"}
    ref = independent_review.owning_program(memory, pr_url, tenant_id=tenant_id) if pr_url else None
    if ref is not None:
        memory.record_program_external_ref(
            str(ref["program_id"]),
            kind="pull_request",
            ref=pr_url,
            state="merged",
            metadata={"merged_by": event.get("merged_by") or "", "merge_sha": event.get("merge_sha") or ""},
            actor="github-webhook",
        )
        result = {"status": "recorded", "program_id": ref["program_id"], "merged_by": event.get("merged_by") or ""}
    try:
        result["base_advanced"] = fan_out_base_advanced(event, tenant_id=tenant_id, memory=memory)
    except Exception as exc:  # noqa: BLE001 - recording the merge must not fail on fan-out
        logger.warning("base_advanced fan-out failed pr_url=%s error=%s", pr_url, exc)
        result["base_advanced_error"] = str(exc)
    return result
