"""Target and guard metrics for the self-improvement loop.

**Target: autonomy.** A ticket is *autonomous* when it merged and no person stepped in before
the merge. The merge itself is a person's (``await_human_merge``) and does not count. A person
stepped in (:func:`interventions`) when any of its tasks shows:

* ``decision_retry`` / ``decision_revise`` / ``decision_accept`` / ``decision_stop``: a decision on
  a stuck step by someone other than the harness (``human_decisions._decision_log``; actors in
  ``human_decisions.HARNESS_ACTORS`` or starting with ``harness-`` are the harness);
* ``jira_reply``: a person answered the ticket's readiness questions on Jira (actor ``jira-reply``);
  the harness had to wait for a person, so it counts;
* ``revise_grant``: a human revise grant (``_revision_grant``, from ``/revise`` or the Inbox);
* ``pr_changes_requested`` / ``pr_closed``: a person requested changes (or ``/revise``) on the PR,
  or closed it;
* ``human_change_requests``: a revision carried change requests from a person;
* ``accepted_by_person``: a person accepted a blocked step as it is;
* ``stopped_by_person``: a person stopped the ticket.

**Guard: problems after merge**, per merged ticket, within ``days`` of the merge (default 14):

* ``reverts``: commits on the PR's base branch that revert its merge commit (``This reverts commit
  <sha>``, ``Reverts owner/repo#N`` or ``Revert "<PR title>"``);
* ``jira_bugs``: Jira bugs created after the merge that link to or mention the ticket key;
* ``smoke_failures``: new nightly smoke failures recorded for the PR's repository after the merge
  (as the web repository, or as the API a web repository's smoke ran against);
* ``arbiter_overruled``: a person decided a step the stuck-step arbiter had already acted on
  (``after_arbiter`` in the decision log); counted for every finished ticket, not only after merge.

GitHub and Jira are read at most ``max_lookups`` times per request and within ``budget_seconds``;
results are cached in this process (an hour while the window is open, a day after it closed). A
signal that could not be read is ``None`` ("unknown"), never an error.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ts_harness.workflows.human_decisions import (
    ACCEPT_KEY,
    DECISION_LOG_KEY,
    STOPPED_PREFIX,
    is_harness_actor,
)

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 14
SIGNALS = ("reverts", "jira_bugs", "smoke_failures", "arbiter_overruled")
INTERVENTION_KINDS = (
    "decision_retry", "decision_revise", "decision_accept", "decision_stop", "jira_reply",
    "revise_grant", "pr_changes_requested", "pr_closed", "human_change_requests",
    "accepted_by_person", "stopped_by_person",
)
MERGE_WAIT_STEPS = frozenset({"await_human_merge"})
JIRA_ACTOR = "jira-reply"
_JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,30}-\d{1,9}$")
_CACHE_LIMIT = 2000
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _input(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("input") if isinstance(task.get("input"), dict) else {}


def _output(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("output") if isinstance(task.get("output"), dict) else {}


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# Autonomy


def interventions(tasks: list[dict[str, Any]]) -> list[str]:
    """The kinds of human intervention a ticket's tasks show (sorted, each once). See the module docstring."""
    from ts_harness.workflows.pr_rejection import PR_CHANGES_REQUESTED_REASON, PR_CLOSED_REASON

    kinds: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        data = _input(task)
        for entry in data.get(DECISION_LOG_KEY) or []:
            if not isinstance(entry, dict):
                continue
            actor = str(entry.get("actor") or "")
            if actor.strip().lower() == JIRA_ACTOR:
                kinds.add("jira_reply")
            elif not is_harness_actor(actor) and str(entry.get("action") or "") in {"retry", "revise", "accept", "stop"}:
                kinds.add(f"decision_{entry['action']}")
        grant = data.get("_revision_grant")
        if isinstance(grant, dict) and not is_harness_actor(str(grant.get("actor") or "")):
            kinds.add("revise_grant")
        if any(str(item).strip() for item in data.get("human_change_requests") or [] if item is not None):
            kinds.add("human_change_requests")
        if str(data.get("_revision_source") or "") in MERGE_WAIT_STEPS:
            kinds.add("pr_changes_requested")
        accepted = data.get(ACCEPT_KEY)
        if isinstance(accepted, dict) and accepted and not is_harness_actor(str(accepted.get("actor") or "")):
            kinds.add("accepted_by_person")
        error = str(task.get("error") or "")
        if error.startswith(STOPPED_PREFIX):
            actor = error[len(STOPPED_PREFIX):].split(":", 1)[0].strip()
            if not is_harness_actor(actor):
                kinds.add("stopped_by_person")
        if error.startswith(PR_CHANGES_REQUESTED_REASON):
            kinds.add("pr_changes_requested")
        elif error.startswith(PR_CLOSED_REASON):
            kinds.add("pr_closed")
    return sorted(kinds)


def arbiter_overrules(tasks: list[dict[str, Any]]) -> int:
    """Decisions by a person on a step the arbiter had already acted on."""
    return sum(
        1
        for task in tasks if isinstance(task, dict)
        for entry in _input(task).get(DECISION_LOG_KEY) or []
        if isinstance(entry, dict) and entry.get("after_arbiter") and not is_harness_actor(str(entry.get("actor") or ""))
    )


# Post-merge problems


def _cached(key: str) -> tuple[bool, Any]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return True, hit[1]
        _cache.pop(key, None)
    return False, None


def _store(key: str, value: Any, ttl: float) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_LIMIT:
            for old in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_LIMIT // 10]:
                _cache.pop(old, None)
        _cache[key] = (time.monotonic() + ttl, value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


@dataclass
class GuardContext:
    """Clients and limits for one metrics request. Factories are called lazily, once each."""

    days: int = DEFAULT_DAYS
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    github_for: Callable[[str], Any] | None = None
    jira_factory: Callable[[], Any] | None = None
    memory: Any = None
    max_lookups: int = 60
    budget_seconds: float = 10.0
    lookups: int = 0
    started: float = field(default_factory=time.monotonic)
    _jira: Any = None
    _jira_failed: bool = False

    def allow(self) -> bool:
        """One more external lookup may run (counted)."""
        if self.lookups >= self.max_lookups or time.monotonic() - self.started > self.budget_seconds:
            return False
        self.lookups += 1
        return True

    def jira(self) -> Any:
        if self._jira is None and not self._jira_failed:
            try:
                if self.jira_factory is not None:
                    self._jira = self.jira_factory()
                else:
                    from ts_harness.integrations.jira_client import JiraClient

                    self._jira = JiraClient.from_env()
            except Exception as exc:  # noqa: BLE001
                logger.info("post-merge guard: Jira unavailable: %s", exc)
                self._jira_failed = True
        return self._jira

    def github(self, repo_url: str) -> Any:
        if self.github_for is not None:
            return self.github_for(repo_url)
        from ts_harness.integrations.github_client import github_for

        return github_for(repo_url)


def _ttl(window_end: datetime, now: datetime) -> float:
    return 86400.0 if now >= window_end else 3600.0


def merge_facts(tasks: list[dict[str, Any]]) -> dict[str, str]:
    """``merge_sha`` and ``merged_by`` from the succeeded merge wait, when the event carried them."""
    for task in sorted(tasks, key=lambda t: str(t.get("created_at") or ""), reverse=True):
        if task.get("step_name") in MERGE_WAIT_STEPS and task.get("status") == "succeeded":
            output = _output(task)
            event = output.get("external_event") if isinstance(output.get("external_event"), dict) else {}
            sha = str(output.get("merge_commit_sha") or output.get("merge_sha") or event.get("merge_commit_sha")
                      or event.get("merge_sha") or "")
            return {"merge_sha": sha, "merged_by": str(output.get("merged_by") or event.get("merged_by") or "")}
    return {"merge_sha": "", "merged_by": ""}


def _is_revert(message: str, *, merge_sha: str, owner: str, repo: str, number: int, title: str) -> bool:
    text = message.lower()
    if merge_sha and len(merge_sha) >= 7:
        for match in re.finditer(r"this reverts commit ([0-9a-f]{7,40})", text):
            sha = match.group(1)
            if merge_sha.lower().startswith(sha) or sha.startswith(merge_sha.lower()):
                return True
    if f"reverts {owner}/{repo}#{number}".lower() in text:
        return True
    return bool(title) and f'revert "{title.lower()}' in text


def reverts(ctx: GuardContext, pr_url: str, merged_at: datetime, merge_sha: str = "") -> int | None:
    """Commits on the PR's base branch within the window that revert it; ``None`` when unknown."""
    from ts_harness.integrations.github_client import parse_github_pr_url

    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        return None
    owner, repo, number = parsed
    end = min(merged_at + timedelta(days=ctx.days), ctx.now)
    key = f"reverts|{pr_url}|{ctx.days}|{end.date().isoformat() if end < ctx.now else ctx.now.strftime('%Y-%m-%dT%H')}"
    hit, value = _cached(key)
    if hit:
        return value
    if not ctx.allow():
        return None
    try:
        github = ctx.github(f"https://github.com/{owner}/{repo}")
        pull = github.get_pull(owner, repo, number) or {}
        base = str((pull.get("base") or {}).get("ref") or "")
        sha = merge_sha or str(pull.get("merge_commit_sha") or "")
        if not base:
            return None
        commits = github.request(
            "GET", f"/repos/{owner}/{repo}/commits",
            query={"sha": base, "since": merged_at.isoformat(), "until": end.isoformat(), "per_page": 100},
        ) or []
        count = sum(
            1 for commit in commits if isinstance(commit, dict)
            and str(commit.get("sha") or "") != sha
            and _is_revert(str((commit.get("commit") or {}).get("message") or ""), merge_sha=sha, owner=owner,
                           repo=repo, number=number, title=str(pull.get("title") or ""))
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("post-merge guard: reverts of %s unknown: %s", pr_url, exc)
        return None
    _store(key, count, _ttl(merged_at + timedelta(days=ctx.days), ctx.now))
    return count


def jira_bugs(ctx: GuardContext, issue_key: str, merged_at: datetime) -> int | None:
    """Jira bugs created in the window that link to or mention ``issue_key``; ``None`` when unknown."""
    key_text = str(issue_key or "").strip().upper()
    if not _JIRA_KEY.match(key_text):
        return None
    end = min(merged_at + timedelta(days=ctx.days), ctx.now)
    cache_key = f"jira|{key_text}|{ctx.days}|{end.date().isoformat() if end < ctx.now else ctx.now.strftime('%Y-%m-%dT%H')}"
    hit, value = _cached(cache_key)
    if hit:
        return value
    if not ctx.allow():
        return None
    jira = ctx.jira()
    if jira is None:
        return None
    fmt = "%Y-%m-%d %H:%M"
    jql = (
        f'issuetype = Bug AND created >= "{merged_at.strftime(fmt)}" AND created <= "{end.strftime(fmt)}" '
        f'AND (issue in linkedIssues("{key_text}") OR text ~ "\\"{key_text}\\"") AND key != "{key_text}"'
    )
    try:
        issues = jira.search(jql, max_results=50, fields=["summary", "created"]) or []
    except Exception as exc:  # noqa: BLE001
        logger.info("post-merge guard: Jira bugs for %s unknown: %s", key_text, exc)
        return None
    count = len([issue for issue in issues if isinstance(issue, dict)])
    _store(cache_key, count, _ttl(merged_at + timedelta(days=ctx.days), ctx.now))
    return count


def _smoke_entries(ctx: GuardContext, kv_key: str) -> dict[str, Any] | None:
    from ts_harness.workflows.nightly_smoke import KV_NAMESPACE

    cache_key = f"smoke|{kv_key}|{ctx.now.strftime('%Y-%m-%dT%H')}"
    hit, value = _cached(cache_key)
    if hit:
        return value
    if not ctx.allow():
        return None
    try:
        memory = ctx.memory
        if memory is None:
            from ts_harness.memory.client import get_memory_client

            memory = get_memory_client()
        value = (memory.kv_get(KV_NAMESPACE, kv_key) or {}).get("value") or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("post-merge guard: smoke record %s unknown: %s", kv_key, exc)
        return None
    failures = value.get("failures") if isinstance(value, dict) else None
    entries = dict(failures) if isinstance(failures, dict) else {}
    _store(cache_key, entries, 3600.0)
    return entries


def smoke_failures(ctx: GuardContext, repo: str, merged_at: datetime) -> int | None:
    """New nightly smoke failures recorded for ``repo`` (``owner/name``) in the window; ``None`` when unknown."""
    from ts_harness.workflows.nightly_smoke import api_index_key, _kv_key

    repo = str(repo or "").strip().lower()
    if "/" not in repo:
        return None
    start, end = merged_at.timestamp(), (merged_at + timedelta(days=ctx.days)).timestamp()
    total = 0
    for kv_key in (_kv_key(repo), api_index_key(repo)):
        entries = _smoke_entries(ctx, kv_key)
        if entries is None:
            return None
        total += sum(1 for entry in entries.values()
                     if isinstance(entry, dict) and start < float(entry.get("at") or 0) <= end)
    return total


def post_merge(ctx: GuardContext, row: dict[str, Any], tasks: list[dict[str, Any]], merged_at: datetime | None) -> dict[str, Any]:
    """The guard signals for one ticket row. Each is a count or ``None`` (unknown / not applicable)."""
    signals: dict[str, Any] = {"arbiter_overruled": arbiter_overrules(tasks)}
    merged = row.get("outcome") == "merged" and merged_at is not None
    if merged:
        facts = merge_facts(tasks)
        signals["reverts"] = reverts(ctx, str(row.get("pr_url") or ""), merged_at, facts["merge_sha"]) if row.get("pr_url") else None
        signals["jira_bugs"] = jira_bugs(ctx, str(row.get("jira_issue_key") or ""), merged_at) if row.get("jira_issue_key") else None
        signals["smoke_failures"] = smoke_failures(ctx, str(row.get("repo") or ""), merged_at) if row.get("repo") else None
    applicable = [name for name in SIGNALS if name in signals and _applies(name, row)]
    unknown = [name for name in applicable if signals.get(name) is None]
    problems = sum(int(signals.get(name) or 0) for name in SIGNALS if name != "arbiter_overruled")
    return {
        **{name: signals.get(name) for name in SIGNALS},
        "problems": problems if merged else 0,
        "unknown": unknown,
        "merged_at": merged_at.isoformat() if merged and merged_at else "",
        "window_open": bool(merged and merged_at and ctx.now < merged_at + timedelta(days=ctx.days)),
    }


def _applies(name: str, row: dict[str, Any]) -> bool:
    return {
        "reverts": bool(row.get("pr_url")),
        "jira_bugs": bool(row.get("jira_issue_key")),
        "smoke_failures": bool(row.get("repo")),
        "arbiter_overruled": True,
    }[name]


def summarize(rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Guard numbers over ticket rows (each with ``guard`` from :func:`post_merge`)."""
    merged = [r for r in rows if r.get("outcome") == "merged"]
    signals: dict[str, Any] = {}
    for name in SIGNALS:
        pool = rows if name == "arbiter_overruled" else merged
        applicable = [r for r in pool if _applies(name, r)]
        known = [r for r in applicable if (r.get("guard") or {}).get(name) is not None]
        signals[name] = {
            "count": sum(int(r["guard"][name]) for r in known) if known or not applicable else None,
            "checked": len(known),
            "unknown": len(applicable) - len(known),
        }
    with_problems = [r for r in merged if int((r.get("guard") or {}).get("problems") or 0) > 0]
    fully_known = [r for r in merged if not (r.get("guard") or {}).get("unknown")]
    return {
        "window_days": days,
        "merged": len(merged),
        "tickets_with_problems": len(with_problems),
        "problem_rate": round(len(with_problems) / len(merged), 3) if merged else None,
        "fully_checked": len(fully_known),
        "window_open": sum(1 for r in merged if (r.get("guard") or {}).get("window_open")),
        "signals": signals,
    }
