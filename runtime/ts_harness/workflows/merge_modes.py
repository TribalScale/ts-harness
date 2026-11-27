"""How a repo keeps a PR up to date with its base at merge time, and the harness's part in it.

Teams run the harness on any GitHub plan, so nothing here may assume a feature. Per repo and
base branch, read-only, best first:

``merge_queue``         GitHub's merge queue tests the PR with the latest base before merging.
``require_up_to_date``  Branch protection (a ruleset or a classic rule) blocks merging a PR that
                        is behind; a person clicks "Update branch".
``harness``             Neither is on, or the harness may not read the settings. Once a person
                        approves the PR, the harness updates a branch that is behind and posts
                        ``harness/up-to-date``. GitHub does not enforce that status, so merging
                        early is not blocked; the console says so.

``merge_readiness.merge_mode`` overrides detection: one mode for every repo, or
``{"default": ..., "org/repo": ...}``; ``auto`` (the default) detects. The harness never changes
a repo's settings, and a failed read means ``harness``, never a failed run.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Any

from ts_harness.integrations.github_client import GitHubClient, GitHubError, parse_github_pr_url

logger = logging.getLogger(__name__)

MODES = ("merge_queue", "require_up_to_date", "harness")
CACHE_SECONDS = 3600
UP_TO_DATE_CONTEXT = "harness/up-to-date"
# Harness statuses that still hold when a branch only gained base-branch commits.
CARRIED_CONTEXTS = ("harness/code-review", "harness/qa", "harness/security-scan", "harness/independent-review")

DESCRIPTIONS = {
    "merge_queue": "GitHub's merge queue tests it with the latest {base} before merging.",
    "require_up_to_date": "GitHub requires it to be up to date with {base} before merging.",
    "harness": (
        "The harness brings it up to date with {base} once it is approved. Merging earlier is not blocked: "
        "turn on branch protection or a merge queue to enforce it."
    ),
}

_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
# Last ``harness/up-to-date`` state posted per head, so the poller posts each one once.
_posted: dict[str, str] = {}


def _override(repo: str, program_defaults: dict[str, Any] | None) -> str:
    raw = dict((program_defaults or {}).get("merge_readiness") or {}).get("merge_mode")
    if isinstance(raw, dict):
        lowered = {str(k).lower(): str(v) for k, v in raw.items()}
        raw = lowered.get(repo.lower()) or lowered.get("default") or "auto"
    value = str(raw or "auto")
    return value if value in MODES else "auto"


def _rules_mode(github: GitHubClient, owner: str, repo: str, branch: str) -> str | None:
    """From rulesets (readable with read access). ``None`` when there are none or they can't be read."""
    quoted = urllib.parse.quote(branch, safe="")
    rules = github.request("GET", f"/repos/{owner}/{repo}/rules/branches/{quoted}", allow_404=True)
    if not isinstance(rules, list):
        return None
    types = {str(rule.get("type") or "") for rule in rules if isinstance(rule, dict)}
    if "merge_queue" in types:
        return "merge_queue"
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            if (rule.get("parameters") or {}).get("strict_required_status_checks_policy"):
                return "require_up_to_date"
    return None


def _protection_mode(github: GitHubClient, owner: str, repo: str, branch: str) -> str | None:
    """From classic branch protection (reading it may need admin rights)."""
    protection = github.branch_protection(owner, repo, branch)
    if isinstance(protection, dict) and (protection.get("required_status_checks") or {}).get("strict"):
        return "require_up_to_date"
    return None


def detect(
    github: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    *,
    program_defaults: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """``{"mode", "source", "base", "description"}`` for one repo and base branch."""
    slug = f"{owner}/{repo}"
    forced = _override(slug, program_defaults)
    if forced != "auto":
        return _result(forced, "override", branch)
    now = time.time() if now is None else now
    key = (str(getattr(github, "host", "")), slug.lower(), branch)
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    mode, source, notes = "", "", []
    for name, read in (("rules", _rules_mode), ("protection", _protection_mode)):
        try:
            found = read(github, owner, repo, branch)
        except GitHubError as exc:
            notes.append(f"{name}: HTTP {exc.status}")
            continue
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the harness mode
            notes.append(f"{name}: {exc}"[:200])
            continue
        if found:
            mode, source = found, name
            break
    result = _result(mode or "harness", source or "fallback", branch, notes)
    _cache[key] = (now + CACHE_SECONDS, result)
    return result


def _result(mode: str, source: str, base: str, notes: list[str] | None = None) -> dict[str, Any]:
    out = {"mode": mode, "source": source, "base": base, "enforced": mode != "harness",
           "description": DESCRIPTIONS[mode].format(base=base or "the base branch")}
    if notes:
        out["notes"] = notes
    return out


def detect_for_pr(
    github: GitHubClient, pr_url: str, base_ref: str, *, program_defaults: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    parsed = parse_github_pr_url(pr_url)
    if parsed is None or not base_ref:
        return None
    return detect(github, parsed[0], parsed[1], base_ref, program_defaults=program_defaults)


# --- Carrying harness statuses over a base-only update ---------------------------------------------


_HUNK = re.compile(r"^@@ [^@]* @@", re.MULTILINE)


def change_identity(files: list[dict[str, Any]], pr_url: str) -> list[list[str]]:
    """What a PR itself changes, independent of where its base is: each file's patch with hunk
    positions removed (a base commit above a hunk moves it), or its blob without a patch."""
    out = []
    for item in files:
        patch = str(item.get("patch") or "")
        body = _HUNK.sub("@@", patch) if patch else "blob:" + str(item.get("sha") or "")
        out.append([pr_url, str(item.get("filename") or ""), str(item.get("previous_filename") or ""),
                    str(item.get("status") or ""), body])
    return sorted(out)


def carry_statuses(
    github: GitHubClient, pr_url: str, *, before_sha: str, after_sha: str, reviewed_identity: list[Any] | None
) -> list[str]:
    """Copy the harness statuses from ``before_sha`` to ``after_sha`` when the PR's own changes are
    still the ones reviewed (``reviewed_identity``, from ``review_precheck``). Returns the contexts
    copied; nothing when the PR changed or nothing was reviewed."""
    parsed = parse_github_pr_url(pr_url)
    if parsed is None or not before_sha or not after_sha or before_sha == after_sha or not reviewed_identity:
        return []
    owner, repo, number = parsed
    reviewed = sorted(list(x) for x in reviewed_identity if isinstance(x, (list, tuple)) and x and x[0] == pr_url)
    if not reviewed or change_identity(github.list_pull_files(owner, repo, number), pr_url) != reviewed:
        return []
    statuses = (github.combined_status(owner, repo, before_sha) or {}).get("statuses") or []
    copied = []
    for status in statuses:
        context = str(status.get("context") or "")
        if context not in CARRIED_CONTEXTS or context in copied:
            continue
        description = str(status.get("description") or "")
        suffix = " (kept after a base update)"
        github.create_status(
            owner, repo, after_sha, state=str(status.get("state") or "pending"), context=context,
            description=(description[: 140 - len(suffix)] + suffix), target_url=status.get("target_url") or None,
        )
        copied.append(context)
    return copied


def post_up_to_date(github: GitHubClient, pr_url: str, head_sha: str, state: str, description: str) -> bool:
    """Post ``harness/up-to-date`` once per head and state."""
    parsed = parse_github_pr_url(pr_url)
    if parsed is None or not head_sha or _posted.get(head_sha) == state:
        return False
    github.create_status(parsed[0], parsed[1], head_sha, state=state, context=UP_TO_DATE_CONTEXT, description=description[:140])
    _posted[head_sha] = state
    return True


def approved(github: GitHubClient, pr_url: str) -> bool:
    """A reviewer's latest verdict is an approval and nobody's is "changes requested"."""
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        return False
    latest: dict[str, str] = {}
    for review in github.list_reviews(*parsed):
        state = str(review.get("state") or "")
        user = str((review.get("user") or {}).get("login") or "")
        if user and state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[user] = state
    return "APPROVED" in latest.values() and "CHANGES_REQUESTED" not in latest.values()


# --- Wiring: webhook, poller --------------------------------------------------------------------


def reviewed_identity(tasks: list[dict[str, Any]]) -> list[Any] | None:
    """The PR changes the newest successful ``review_precheck`` saw."""
    prechecks = sorted(
        (t for t in tasks if t.get("step_name") == "review_precheck" and t.get("status") == "succeeded"
         and isinstance((t.get("output") or {}).get("change_identity"), list)),
        key=lambda t: str(t.get("created_at") or ""),
    )
    return prechecks[-1]["output"]["change_identity"] if prechecks else None


def _merge_waits_for(memory: Any, pr_url: str, tenant_id: str) -> list[dict[str, Any]]:
    from ts_harness.workflows.merge_readiness import _wait_pr_urls

    waits = list((memory.find_external_waits(event_type="github.merge", tenant_id=tenant_id) or {}).get("waits") or [])
    return [wait for wait in waits if pr_url in _wait_pr_urls(wait)]


def carry_on_update(event: dict[str, Any], *, tenant_id: str, memory: Any = None, github: GitHubClient | None = None) -> list[str]:
    """``synchronize`` on a PR waiting to be merged (someone clicked "Update branch", or the harness
    updated it): keep the harness statuses when only base commits came in."""
    pr_url, before, after = str(event.get("pr_url") or ""), str(event.get("before_sha") or ""), str(event.get("head_sha") or "")
    if not pr_url or not before or not after:
        return []
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    waits = _merge_waits_for(memory, pr_url, tenant_id)
    if not waits:
        return []
    from ts_harness.workflows.hooks._helpers import load_workflow_payload

    _workflow, tasks = load_workflow_payload(memory, str(waits[0].get("workflow_id") or ""))
    github = github or GitHubClient.from_env()
    return carry_statuses(github, pr_url, before_sha=before, after_sha=after, reviewed_identity=reviewed_identity(tasks))


def _wait_new_head(github: GitHubClient, parsed: tuple[str, str, int], old: str, sleep: Any, tries: int = 5) -> str:
    head = old
    for _ in range(tries):
        sleep(2)
        head = str((github.get_pull(*parsed).get("head") or {}).get("sha") or "")
        if head and head != old:
            return head
    return head


def keep_approved_current(
    *,
    tenant_id: str = "default",
    github: GitHubClient | None = None,
    memory: Any = None,
    program_defaults: dict[str, Any] | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """The ``harness`` mode, once a minute: post ``harness/up-to-date`` on each waiting PR and bring an
    approved PR that fell behind up to date (GitHub merges the base in; a conflict is left to
    ``send_back_conflicting_merges``). Repos with a merge queue or the up-to-date rule are skipped."""
    from ts_harness.workflows.hooks._helpers import load_workflow_payload
    from ts_harness.workflows.merge_readiness import _live_program_defaults, _wait_pr_urls, pr_base_state

    if github is None:
        return {"checked": 0, "updated": 0}
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    defaults = program_defaults if program_defaults is not None else _live_program_defaults()
    waits = list((memory.find_external_waits(event_type="github.merge", tenant_id=tenant_id) or {}).get("waits") or [])
    checked = updated = errors = 0
    for wait in waits:
        for pr_url in _wait_pr_urls(wait):
            parsed = parse_github_pr_url(pr_url)
            if parsed is None:
                continue
            try:
                state = pr_base_state(github, pr_url)
                if state["status"] in {"skipped", "conflict"}:
                    continue
                mode = detect(github, parsed[0], parsed[1], state["base_ref"], program_defaults=defaults)
                if mode["mode"] != "harness":
                    continue
                checked += 1
                base, head = state["base_ref"], state["head_sha"]
                if state["status"] == "fresh":
                    post_up_to_date(github, pr_url, head, "success", f"Up to date with {base}.")
                    continue
                if not approved(github, pr_url):
                    post_up_to_date(github, pr_url, head, "pending",
                                    f"Behind {base}; merges cleanly. Brought up to date once approved.")
                    continue
                try:
                    github.update_branch(*parsed, expected_head_sha=head)
                except GitHubError as exc:
                    if exc.status == 422:  # the head moved or it conflicts: the next minute sees it
                        continue
                    raise
                new_head = _wait_new_head(github, parsed, head, sleep)
                if new_head and new_head != head:
                    _workflow, tasks = load_workflow_payload(memory, str(wait.get("workflow_id") or ""))
                    carry_statuses(github, pr_url, before_sha=head, after_sha=new_head, reviewed_identity=reviewed_identity(tasks))
                    post_up_to_date(github, pr_url, new_head, "success", f"Brought up to date with {base} after approval.")
                updated += 1
                logger.info("approved PR brought up to date pr_url=%s base=%s", pr_url, base)
            except Exception as exc:  # noqa: BLE001 - one PR must not stop the others
                errors += 1
                logger.warning("keeping %s up to date failed: %s", pr_url, exc)
    return {"checked": checked, "updated": updated, "errors": errors}
