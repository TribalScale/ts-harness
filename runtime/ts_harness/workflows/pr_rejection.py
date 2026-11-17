"""Human rejection of a PR that is waiting for its merge.

* Closed without merging: the work order stops.
* A "Request changes" review on the current head commit, or a ``/revise <feedback>``
  comment posted after it: the work order goes back to the coding backend.

Only people with write access count, and each signal is tied to the head commit, so
the revision's new commits make it stale and it cannot loop.
"""

from __future__ import annotations

from typing import Any

PR_CLOSED_REASON = "external_blocked:github.pr_closed"
PR_CHANGES_REQUESTED_REASON = "external_blocked:github.pr_changes_requested"
REJECTION_REASONS = (PR_CLOSED_REASON, PR_CHANGES_REQUESTED_REASON)
REVISE_COMMAND = "/revise"
WRITE_ROLES = frozenset({"admin", "maintain", "write"})
# COMMENTED reviews do not change a reviewer's verdict on GitHub.
_VERDICT_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


def _login(user: Any) -> str:
    return str((user or {}).get("login") or "") if isinstance(user, dict) else ""


def detect_rejection(github: Any, owner: str, repo: str, number: int, pull: dict[str, Any]) -> dict[str, Any] | None:
    """``{"reason", "actor", "feedback"}`` when a human rejected the PR, else ``None``."""
    if pull.get("merged"):
        return None
    if pull.get("state") == "closed":
        closed_by = _login((github.get_issue(owner, repo, number) or {}).get("closed_by"))
        return {
            "reason": f"{PR_CLOSED_REASON} by {closed_by}" if closed_by else PR_CLOSED_REASON,
            "actor": closed_by,
            "feedback": "",
        }
    head_sha = str((pull.get("head") or {}).get("sha") or "")
    if not head_sha:
        return None
    can_write = _write_access_checker(github, owner, repo)
    found = _changes_requested(github, owner, repo, number, head_sha, can_write) or _revise_command(
        github, owner, repo, number, head_sha, can_write
    )
    if found is None:
        return None
    actor, details = found
    return {
        "reason": f"{PR_CHANGES_REQUESTED_REASON} by {actor}",
        "actor": actor,
        "feedback": f"Human reviewer {actor} requested changes on the pull request:\n{details or '(no details given)'}",
    }


def _write_access_checker(github: Any, owner: str, repo: str):
    cache: dict[str, bool] = {}

    def can_write(login: str) -> bool:
        if login not in cache:
            cache[login] = bool(login) and github.collaborator_permission(owner, repo, login) in WRITE_ROLES
        return cache[login]

    return can_write


def _changes_requested(github, owner, repo, number, head_sha, can_write) -> tuple[str, str] | None:
    latest: dict[str, dict[str, Any]] = {}
    for review in github.list_reviews(owner, repo, number):
        login = _login(review.get("user"))
        if login and str(review.get("state") or "") in _VERDICT_STATES:
            latest[login] = review
    for login, review in sorted(latest.items(), key=lambda item: str(item[1].get("submitted_at") or ""), reverse=True):
        if review.get("state") != "CHANGES_REQUESTED" or review.get("commit_id") != head_sha or not can_write(login):
            continue
        lines = [str(review.get("body") or "").strip()]
        for comment in github.list_review_comments(owner, repo, number, review.get("id")):
            line = comment.get("line") or comment.get("original_line")
            where = f"{comment.get('path')}:{line}" if line else str(comment.get("path") or "")
            lines.append(f"- {where}: {str(comment.get('body') or '').strip()}")
        return login, "\n".join(line for line in lines if line)
    return None


def _revise_command(github, owner, repo, number, head_sha, can_write) -> tuple[str, str] | None:
    commit = (github.get_commit(owner, repo, head_sha) or {}).get("commit") or {}
    pushed_at = str((commit.get("committer") or {}).get("date") or "")
    comments = github.list_issue_comments(owner, repo, number, since=pushed_at or None)
    for comment in sorted(comments, key=lambda c: str(c.get("created_at") or ""), reverse=True):
        body = str(comment.get("body") or "").strip()
        login = _login(comment.get("user"))
        if not body.startswith(REVISE_COMMAND) or str(comment.get("created_at") or "") < pushed_at:
            continue
        rest = body[len(REVISE_COMMAND):]
        if rest[:1] not in {"", " ", "\n", "\r", "\t", ":"} or not can_write(login):
            continue
        return login, rest.lstrip(":").strip()
    return None
