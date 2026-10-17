"""Undo what a cancelled program left behind in Jira and GitHub.

Reads ``program_external_refs`` for the program and all its descendants:

* Jira issues get the ``harness-cancelled`` label and a comment; they are transitioned
  only when ``compensation.jira`` is ``close``.
* Open pull requests are commented on and closed. Merged ones are left alone.
* Branches are deleted only when TS Harness created them, their PR is closed and not
  merged, and ``compensation.delete_branches`` is true (default false).
* GitHub refs outside the tenant binding (``github.org`` / ``repo_picker``) are
  skipped as ``repo_not_bound``; with no binding configured nothing is closed or
  deleted. Jira issues outside ``jira_filing.allowed_projects`` are skipped too.
* Deployments are never touched; release rollback is a separate program node.

Every action is recorded on the program (``compensation`` artifact) and audited.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ts_harness.config.bindings import (
    TenantBindings,
    check_jira_projects_allowed,
    check_repo_allowed,
    tenant_bindings,
)
from ts_harness.integrations.github_client import GitHubClient, parse_github_pr_url

logger = logging.getLogger(__name__)

CANCELLED_LABEL = "harness-cancelled"
DEFAULT_CLOSE_TRANSITION = "Done"


def compensation_settings(defaults: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict((defaults or {}).get("compensation") or {})
    return {
        "jira": raw.get("jira") if raw.get("jira") in {"label", "close"} else "label",
        "jira_close_transition": str(raw.get("jira_close_transition") or DEFAULT_CLOSE_TRANSITION),
        "delete_branches": raw.get("delete_branches") is True,
    }


class _Lazy:
    """Build an integration client on first use so a missing token only fails its own refs."""

    def __init__(self, factory: Callable[[], Any], client: Any = None):
        self._factory = factory
        self._client = client

    def get(self) -> Any:
        if self._client is None:
            self._client = self._factory()
        return self._client


def _jira_factory() -> Any:
    from ts_harness.integrations.jira_client import JiraClient

    return JiraClient.from_env()


def _action(ref: dict[str, Any], action: str, result: str, detail: str = "") -> dict[str, Any]:
    return {
        "kind": ref.get("kind"),
        "ref": ref.get("ref"),
        "program_id": ref.get("program_id"),
        "action": action,
        "result": result,
        "detail": detail,
    }


def _repo_not_bound(owner: str, repo: str, bindings: TenantBindings) -> str | None:
    if not bindings.github_org and not bindings.allowed_repos:
        return "repo_not_bound: no tenant GitHub binding is configured"
    error = check_repo_allowed(f"{owner}/{repo}", bindings)
    return f"repo_not_bound: {error}" if error else None


def _record_state(memory: Any, ref: dict[str, Any], state: str, actor: str, **metadata: Any) -> None:
    try:
        memory.record_program_external_ref(
            str(ref["program_id"]),
            kind=str(ref["kind"]),
            ref=str(ref["ref"]),
            state=state,
            metadata={key: value for key, value in metadata.items() if value is not None},
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("external ref state not recorded for %s: %s", ref.get("ref"), exc)


def _compensate_jira(
    memory: Any,
    ref: dict[str, Any],
    jira: _Lazy,
    settings: dict[str, Any],
    note: str,
    actor: str,
    bindings: TenantBindings,
) -> dict[str, Any]:
    if ref.get("state") == "closed":
        return _action(ref, "none", "skipped", "already closed")
    labelled = ref.get("state") == "labelled"
    if labelled and settings["jira"] != "close":
        return _action(ref, "none", "skipped", "already labelled")
    key = str(ref["ref"])
    outside = check_jira_projects_allowed({key.split("-", 1)[0].upper()}, bindings)
    if outside:
        return _action(ref, "none", "skipped", f"project_not_bound: {outside}")
    client = jira.get()
    if not labelled:
        client.add_label(key, CANCELLED_LABEL)
        client.comment(key, note)
    if settings["jira"] != "close":
        _record_state(memory, ref, "labelled", actor, compensation="labelled")
        return _action(ref, "label_and_comment", "done")
    if client.transition(key, settings["jira_close_transition"]):
        _record_state(memory, ref, "closed", actor, compensation="closed")
        return _action(ref, "label_comment_and_close", "done")
    _record_state(memory, ref, "labelled", actor, compensation="labelled")
    return _action(ref, "label_comment_and_close", "failed", f"no transition named {settings['jira_close_transition']}")


def _compensate_pull(
    memory: Any, ref: dict[str, Any], github: _Lazy, note: str, actor: str, bindings: TenantBindings
) -> tuple[dict[str, Any], str]:
    """Returns the action and the PR's final state (open, closed or merged)."""
    parsed = parse_github_pr_url(str(ref["ref"]))
    if parsed is None:
        return _action(ref, "none", "skipped", "not a GitHub PR URL"), str(ref.get("state") or "")
    owner, repo, number = parsed
    unbound = _repo_not_bound(owner, repo, bindings)
    if unbound:
        return _action(ref, "none", "skipped", unbound), str(ref.get("state") or "")
    client = github.get()
    pull = client.get_pull(owner, repo, number) or {}
    if pull.get("merged") or pull.get("merged_at"):
        _record_state(memory, ref, "merged", actor)
        return _action(ref, "none", "skipped", "already merged"), "merged"
    if str(pull.get("state") or "") == "closed":
        _record_state(memory, ref, "closed", actor)
        return _action(ref, "none", "skipped", "already closed"), "closed"
    client.comment(owner, repo, number, note)
    client.close_pull(owner, repo, number)
    _record_state(memory, ref, "closed", actor, compensation="closed")
    return _action(ref, "comment_and_close", "done"), "closed"


def _branch_pr_state(branch_ref: dict[str, Any], pr_states: dict[str, str], pr_refs: list[dict[str, Any]]) -> str | None:
    meta = dict(branch_ref.get("metadata") or {})
    for pr in pr_refs:
        pr_meta = dict(pr.get("metadata") or {})
        same_branch = pr_meta.get("branch") and pr_meta.get("branch") == meta.get("branch")
        same_repo = not meta.get("repo") or pr_meta.get("repo") == meta.get("repo")
        if same_branch and same_repo:
            return pr_states.get(str(pr["ref"]), str(pr.get("state") or ""))
    return None


def _compensate_branch(
    memory: Any,
    ref: dict[str, Any],
    github: _Lazy,
    settings: dict[str, Any],
    pr_state: str | None,
    actor: str,
    bindings: TenantBindings,
) -> dict[str, Any]:
    if ref.get("state") == "deleted":
        return _action(ref, "none", "skipped", "already deleted")
    if not settings["delete_branches"]:
        return _action(ref, "none", "skipped", "compensation.delete_branches is false")
    meta = dict(ref.get("metadata") or {})
    if meta.get("created_by_harness") is not True:
        return _action(ref, "none", "skipped", "branch was not created by TS Harness")
    if pr_state != "closed":
        return _action(ref, "none", "skipped", f"pull request is {pr_state or 'missing'}, not closed")
    repo_slug, _, branch = str(ref["ref"]).partition("#")
    owner, _, repo = (meta.get("repo") or repo_slug).partition("/")
    branch = meta.get("branch") or branch
    if not (owner and repo and branch):
        return _action(ref, "none", "skipped", "branch ref is not owner/repo#branch")
    unbound = _repo_not_bound(owner, repo, bindings)
    if unbound:
        return _action(ref, "none", "skipped", unbound)
    github.get().delete_branch(owner, repo, branch)
    _record_state(memory, ref, "deleted", actor, compensation="deleted")
    return _action(ref, "delete_branch", "done")


def _status(actions: list[dict[str, Any]]) -> str:
    done = any(item["result"] == "done" for item in actions)
    failed = any(item["result"] == "failed" for item in actions)
    if failed:
        return "partial" if done else "failed"
    return "completed" if done else "skipped"


def compensate_cancelled_program(
    memory: Any,
    program_id: str,
    *,
    reason: str = "",
    actor: str = "orchestrator",
    defaults: dict[str, Any] | None = None,
    github: Any = None,
    jira: Any = None,
) -> dict[str, Any]:
    settings = compensation_settings(defaults)
    bindings = tenant_bindings(defaults)
    refs = list((memory.list_program_external_refs(program_id, include_descendants=True) or {}).get("refs") or [])
    github_client = _Lazy(GitHubClient.from_env, github)
    jira_client = _Lazy(_jira_factory, jira)
    note = f"TS Harness cancelled this delivery program{f': {reason}' if reason else ''}."
    actions: list[dict[str, Any]] = []

    def attempt(ref: dict[str, Any], label: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel compensation failed for %s %s: %s", ref.get("kind"), ref.get("ref"), exc)
            actions.append(_action(ref, label, "failed", str(exc)[:300]))
            return None

    for ref in refs:
        if ref.get("kind") == "jira_issue":
            result = attempt(ref, "label_and_comment", lambda ref=ref: _compensate_jira(memory, ref, jira_client, settings, note, actor, bindings))
            if result:
                actions.append(result)

    pr_refs = [ref for ref in refs if ref.get("kind") == "pull_request"]
    pr_states: dict[str, str] = {}
    for ref in pr_refs:
        if ref.get("state") in {"merged", "closed"}:
            pr_states[str(ref["ref"])] = str(ref["state"])
            actions.append(_action(ref, "none", "skipped", f"already {ref['state']}"))
            continue
        result = attempt(ref, "comment_and_close", lambda ref=ref: _compensate_pull(memory, ref, github_client, note, actor, bindings))
        if result:
            action, state = result
            pr_states[str(ref["ref"])] = state
            actions.append(action)

    for ref in refs:
        if ref.get("kind") == "branch":
            pr_state = _branch_pr_state(ref, pr_states, pr_refs)
            result = attempt(
                ref,
                "delete_branch",
                lambda ref=ref, pr_state=pr_state: _compensate_branch(memory, ref, github_client, settings, pr_state, actor, bindings),
            )
            if result:
                actions.append(result)

    status = _status(actions)
    try:
        memory.record_program_compensation(program_id, status=status, actions=actions, actor=actor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("compensation result not recorded for program %s: %s", program_id, exc)
    return {"program_id": program_id, "status": status, "actions": actions}
