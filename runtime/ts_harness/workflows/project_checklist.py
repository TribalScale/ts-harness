"""Setup checklist for a project: each item a new project needs, checked live.

Each check is a plain function over a ``ChecklistContext`` whose clients (Jira, GitHub,
the App installation lookup) are injectable, so tests use fakes. ``run_checklist`` runs
the checks concurrently, each time-boxed; a check that raises or runs out of time
becomes a ``warning`` item that says it could not check, and never fails the others.

Each item is ``{id, title, status, summary, details, fix, fix_link}`` with ``status``
one of ``ok``, ``warning`` or ``failing``. ``fix_link`` is a console route (a ``#/...``
hash) when there is a page to fix the item on.
"""

from __future__ import annotations

import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

OK, WARNING, FAILING = "ok", "warning", "failing"
DEFAULT_TIMEOUT_SECONDS = 10.0

BDD_MODES = ("auto", "always", "never")
REVISION_AUTONOMY = ("autonomous", "standard", "conservative")
MAX_PARALLEL_RANGE = (1, 8)

LINK_JIRA_DEFAULTS = "#/system/settings"
LINK_IDENTITY = "#/system/settings/identity"
LINK_GITHUB = "#/system/github"


def project_link(project_id: str) -> str:
    return f"#/system/projects/{urllib.parse.quote(str(project_id), safe='')}"


@dataclass
class ChecklistContext:
    project: dict[str, Any]
    repositories: list[dict[str, Any]]
    program_defaults: dict[str, Any] = field(default_factory=dict)
    github_logins: dict[str, str] = field(default_factory=dict)
    jira_account_ids: dict[str, str] = field(default_factory=dict)
    tenant_id: str = "default"
    requester: str = ""
    # () -> Jira client (``request(method, path)``); raising means Jira is not configured.
    jira: Callable[[], Any] | None = None
    # repo_url -> GitHub client (``request``, ``required_status_contexts``, ``get_tree``, ``get_file``).
    github: Callable[[str], Any] | None = None
    # (host, owner, repo) -> installation id, None when the App is registered but not installed,
    # or "no_app" when no App is registered for the host.
    installation: Callable[[str, str, str], Any] | None = None
    memory: Any = None


def _item(
    id: str, title: str, status: str, summary: str, *, details: list[str] | None = None, fix: str = "", fix_link: str = ""
) -> dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "status": status,
        "summary": summary,
        "details": list(details or []),
        "fix": fix if status != OK else "",
        "fix_link": fix_link if status != OK else "",
    }


def _worst(statuses: list[str]) -> str:
    if FAILING in statuses:
        return FAILING
    if WARNING in statuses:
        return WARNING
    return OK


def _jira_key(ctx: ChecklistContext) -> str:
    return str(ctx.project.get("jira_project_key") or "").strip().upper()


def _repo_parts(repo: dict[str, Any]) -> tuple[str, str, str, str]:
    full_name = str(repo.get("full_name") or "")
    owner, _, name = full_name.partition("/")
    host = str(repo.get("github_host") or "github.com").lower()
    url = str(repo.get("repo_url") or f"https://{host}/{full_name}")
    return host, owner, name, url


def _branch(ctx: ChecklistContext, repo: dict[str, Any]) -> str:
    return str(repo.get("default_branch") or ctx.project.get("default_base_branch") or "dev")


def _status_of(exc: Exception) -> int:
    return int(getattr(exc, "status", 0) or 0)


# ---------------------------------------------------------------------------
# 1. Jira project key and the filing allowlist
# ---------------------------------------------------------------------------


def check_jira_allowed(ctx: ChecklistContext) -> dict[str, Any]:
    from ts_harness.config.bindings import tenant_bindings

    title = "Jira project allowed"
    key = _jira_key(ctx)
    if not key:
        return _item("jira_allowed", title, FAILING, "The project has no Jira project key.",
                     fix="Set the Jira project key in Delivery on this page.", fix_link=project_link(ctx.project.get("id", "")))
    allowed = tenant_bindings(ctx.program_defaults).jira_allowed_projects
    if not allowed:
        return _item("jira_allowed", title, OK, f"{key} is set; no Jira allowlist is configured, so every project is allowed.")
    if key in allowed:
        return _item("jira_allowed", title, OK, f"{key} is in the Jira filing allowlist.")
    return _item(
        "jira_allowed", title, FAILING,
        f"{key} is not in System Settings → Jira filing → Allowed projects ({', '.join(allowed)}); Jira writes to it are blocked.",
        fix=f"Add {key} to Allowed projects in System Settings → Jira defaults.", fix_link=LINK_JIRA_DEFAULTS,
    )


# ---------------------------------------------------------------------------
# 2. Jira reachable and the project exists
# ---------------------------------------------------------------------------


def check_jira_project(ctx: ChecklistContext) -> dict[str, Any]:
    title = "Jira reachable"
    key = _jira_key(ctx)
    if not key:
        return _item("jira_project", title, WARNING, "Skipped: the project has no Jira project key.",
                     fix="Set the Jira project key in Delivery on this page.", fix_link=project_link(ctx.project.get("id", "")))
    if ctx.jira is None:
        return _item("jira_project", title, WARNING, "Jira is not configured for this install.",
                     fix="Set up the Jira service account (docs/runbooks/jira-service-account.md).")
    try:
        client = ctx.jira()
    except Exception as exc:  # noqa: BLE001 - missing credentials surface as the item
        return _item("jira_project", title, FAILING, f"Jira credentials are not usable: {str(exc)[:200]}",
                     fix="Set up the Jira service account (docs/runbooks/jira-service-account.md).")
    try:
        project = client.request("GET", f"/rest/api/3/project/{urllib.parse.quote(key, safe='')}") or {}
    except Exception as exc:  # noqa: BLE001
        status = _status_of(exc)
        if status == 404:
            return _item("jira_project", title, FAILING,
                         f"Jira is reachable but project {key} does not exist or the service account cannot browse it.",
                         fix=f"Check the key, or give the Jira service account Browse access to {key}.")
        if status in {401, 403}:
            return _item("jira_project", title, FAILING, f"Jira rejected the service account (HTTP {status}).",
                         fix="Rotate or re-grant the Jira service account (docs/runbooks/jira-service-account.md).")
        raise
    name = str(project.get("name") or key)
    return _item("jira_project", title, OK, f"Jira is reachable and {key} ({name}) exists.")


# ---------------------------------------------------------------------------
# 3. Repositories linked, the App can reach each, and its base branch exists
# ---------------------------------------------------------------------------


def _repo_access(ctx: ChecklistContext, repo: dict[str, Any]) -> tuple[str, str]:
    host, owner, name, url = _repo_parts(repo)
    branch = _branch(ctx, repo)
    installation = ctx.installation(host, owner, name) if ctx.installation is not None else "no_app"
    via_pat, no_app = installation is None, installation == "no_app"
    if ctx.github is None:
        return WARNING, f"{owner}/{name}: no GitHub credentials configured"
    github = ctx.github(url)
    try:
        github.request("GET", f"/repos/{owner}/{name}")
    except Exception as exc:  # noqa: BLE001
        if _status_of(exc) in {401, 403, 404}:
            reason = (
                "the GitHub token cannot read it" if no_app
                else "the GitHub App is not installed on it" if via_pat
                else "the GitHub App cannot read it"
            )
            return FAILING, f"{owner}/{name}: {reason} (HTTP {_status_of(exc)})"
        raise
    found = github.request("GET", f"/repos/{owner}/{name}/branches/{urllib.parse.quote(branch, safe='')}", allow_404=True)
    if found is None:
        return FAILING, f"{owner}/{name}: base branch {branch} does not exist"
    if via_pat:
        return WARNING, f"{owner}/{name}: readable only through the fallback token; the GitHub App is not installed on it"
    if no_app:
        return OK, f"{owner}/{name}: the GitHub token has access (no GitHub App registered); {branch} exists"
    return OK, f"{owner}/{name}: App has access; {branch} exists"


def check_repositories(ctx: ChecklistContext) -> dict[str, Any]:
    title = "Repositories linked and reachable"
    if not ctx.repositories:
        return _item("repositories", title, FAILING, "No repository is linked to this project.",
                     fix="Add a repository under Repositories on this page.", fix_link=project_link(ctx.project.get("id", "")))
    statuses, details = [], []
    for repo in ctx.repositories:
        try:
            status, line = _repo_access(ctx, repo)
        except Exception as exc:  # noqa: BLE001 - one repository's error does not hide the others
            status, line = WARNING, f"{repo.get('full_name')}: could not check: {str(exc)[:200]}"
        statuses.append(status)
        details.append(line)
    status = _worst(statuses)
    ready = statuses.count(OK)
    summary = (
        f"{len(statuses)} repositor{'y' if len(statuses) == 1 else 'ies'} linked; the App reaches each and its base branch exists."
        if status == OK else f"{ready} of {len(statuses)} repositories ready."
    )
    return _item("repositories", title, status, summary, details=details,
                 fix="Install the GitHub App on each repository (System → GitHub) and create the base branch, or change it on this page.",
                 fix_link=LINK_GITHUB)


# ---------------------------------------------------------------------------
# 4. Repositories allowed by the tenant binding
# ---------------------------------------------------------------------------


def check_repo_allowed(ctx: ChecklistContext) -> dict[str, Any]:
    from ts_harness.config.bindings import check_repo_allowed_or_linked, tenant_bindings

    title = "Repositories allowed"
    if not ctx.repositories:
        return _item("repo_allowed", title, WARNING, "No repositories to check.",
                     fix="Add a repository under Repositories on this page.", fix_link=project_link(ctx.project.get("id", "")))
    bindings = tenant_bindings(ctx.program_defaults)
    problems = []
    for repo in ctx.repositories:
        error = check_repo_allowed_or_linked(_repo_parts(repo)[3], bindings, tenant_id=ctx.tenant_id, memory=ctx.memory)
        if error:
            problems.append(error)
    if problems:
        return _item("repo_allowed", title, FAILING, "Some repositories are outside the tenant binding; coding runs on them are refused.",
                     details=problems,
                     fix="Use repositories in the tenant GitHub org, or change the GitHub org in System Settings → Jira defaults → Delivery defaults.",
                     fix_link=LINK_JIRA_DEFAULTS)
    org = bindings.github_org
    summary = f"Every repository is in the tenant GitHub org {org}." if org else "No tenant GitHub org is set; linked repositories are allowed."
    return _item("repo_allowed", title, OK, summary)


# ---------------------------------------------------------------------------
# 5. Branch protection requires the harness checks
# ---------------------------------------------------------------------------


class _MemoContexts:
    """Reads branch protection once per repository however many checks ask."""

    def __init__(self, github: Any):
        self._github = github
        self._cache: dict[tuple[str, str, str], Any] = {}

    def required_status_contexts(self, owner: str, repo: str, branch: str) -> list[str] | None:
        key = (owner, repo, branch)
        if key not in self._cache:
            self._cache[key] = self._github.required_status_contexts(owner, repo, branch)
        return self._cache[key]


def required_contexts(program_defaults: dict[str, Any] | None) -> list[str]:
    """The harness statuses the base branch must require (as docs/runbooks/github-branch-protection.md)."""
    from ts_harness.config.governance import governance_snapshot, independent_review_enabled
    from ts_harness.workflows import independent_review
    from ts_harness.workflows.merge_readiness import REQUIRED_CLAW_CONTEXTS

    names = list(REQUIRED_CLAW_CONTEXTS)
    if independent_review_enabled(governance_snapshot(program_defaults)):
        names.append(independent_review.STATUS_CONTEXT)
    return names


def _protection(ctx: ChecklistContext, repo: dict[str, Any], wanted: list[str]) -> tuple[str, str]:
    from ts_harness.workflows import independent_review

    _host, owner, name, url = _repo_parts(repo)
    branch = _branch(ctx, repo)
    github = _MemoContexts(ctx.github(url))  # type: ignore[misc]
    contexts = github.required_status_contexts(owner, name, branch)
    if contexts is None:
        return FAILING, f"{owner}/{name}@{branch} is not protected; no harness check is enforced"
    # The same warning the delivery flow logs for harness/independent-review.
    warnings = independent_review.protection_warnings(
        github, owner, name, branch, enabled=independent_review.STATUS_CONTEXT in wanted  # type: ignore[arg-type]
    )
    missing = [n for n in wanted if n not in contexts and n != independent_review.STATUS_CONTEXT]
    if missing:
        warnings.insert(0, f"{owner}/{name}@{branch} branch protection does not require: {', '.join(missing)}")
    if warnings:
        return FAILING, "; ".join(warnings)
    return OK, f"{owner}/{name}@{branch} requires {', '.join(wanted)}"


def check_branch_protection(ctx: ChecklistContext) -> dict[str, Any]:
    title = "Branch protection requires harness checks"
    if not ctx.repositories:
        return _item("branch_protection", title, WARNING, "No repositories to check.",
                     fix="Add a repository under Repositories on this page.", fix_link=project_link(ctx.project.get("id", "")))
    if ctx.github is None:
        return _item("branch_protection", title, WARNING, "Could not check: no GitHub credentials configured.", fix_link=LINK_GITHUB,
                     fix="Register the GitHub App (System → GitHub).")
    wanted = required_contexts(ctx.program_defaults)
    statuses, details = [], []
    for repo in ctx.repositories:
        try:
            status, line = _protection(ctx, repo, wanted)
        except Exception as exc:  # noqa: BLE001
            status, line = WARNING, f"{repo.get('full_name')}: could not read branch protection: {str(exc)[:200]}"
        statuses.append(status)
        details.append(line)
    status = _worst(statuses)
    summary = (
        f"Each base branch requires {', '.join(wanted)}." if status == OK
        else f"{statuses.count(OK)} of {len(statuses)} base branches require {', '.join(wanted)}."
    )
    return _item("branch_protection", title, status, summary, details=details,
                 fix=f"Protect each base branch and require {', '.join(wanted)} plus the repo's CI "
                     "(docs/runbooks/github-branch-protection.md; scripts/repo-governance.py --apply sets it).")


# ---------------------------------------------------------------------------
# 6. CI runs on pull requests
# ---------------------------------------------------------------------------


def check_ci(ctx: ChecklistContext) -> dict[str, Any]:
    from ts_harness.workflows.repo_bootstrap import _has_pr_ci

    title = "CI on pull requests"
    if not ctx.repositories:
        return _item("ci_on_pull_requests", title, WARNING, "No repositories to check.",
                     fix="Add a repository under Repositories on this page.", fix_link=project_link(ctx.project.get("id", "")))
    if ctx.github is None:
        return _item("ci_on_pull_requests", title, WARNING, "Could not check: no GitHub credentials configured.",
                     fix="Register the GitHub App (System → GitHub).", fix_link=LINK_GITHUB)
    statuses, details = [], []
    for repo in ctx.repositories:
        _host, owner, name, url = _repo_parts(repo)
        branch = _branch(ctx, repo)
        try:
            has_ci = _has_pr_ci(ctx.github(url), owner, name, branch)
        except Exception as exc:  # noqa: BLE001
            has_ci = None
            details.append(f"{owner}/{name}: could not check: {str(exc)[:200]}")
            statuses.append(WARNING)
            continue
        if has_ci is True:
            statuses.append(OK)
            details.append(f"{owner}/{name}: a workflow runs on pull requests into {branch}")
        elif has_ci is False:
            statuses.append(WARNING)
            details.append(f"{owner}/{name}: no workflow runs on pull requests into {branch}; planning adds an \"add CI\" work order first")
        else:
            statuses.append(WARNING)
            details.append(f"{owner}/{name}: could not read its workflows")
    status = _worst(statuses)
    summary = "Every repository runs CI on pull requests." if status == OK else f"{statuses.count(OK)} of {len(statuses)} repositories run CI on pull requests."
    return _item("ci_on_pull_requests", title, status, summary, details=details,
                 fix="Add a GitHub Actions workflow triggered on pull_request into the base branch (docs/runbooks/onboard-a-repo.md).")


# ---------------------------------------------------------------------------
# 7. People mapped to GitHub (and Jira) accounts
# ---------------------------------------------------------------------------


def check_identity(ctx: ChecklistContext) -> dict[str, Any]:
    title = "Approvers mapped to GitHub and Jira"
    logins = {str(k).lower(): v for k, v in (ctx.github_logins or {}).items() if v}
    accounts = {str(k).lower(): v for k, v in (ctx.jira_account_ids or {}).items() if v}
    who = str(ctx.requester or "").strip().lower()
    details: list[str] = []
    if not logins:
        return _item("identity", title, FAILING,
                     "No one has a GitHub login mapping; PR approvals cannot be matched to people.",
                     fix="Add each approver's email and GitHub login in System Settings → Identity → GitHub logins.",
                     fix_link=LINK_IDENTITY)
    status = OK
    if "@" in who:
        if who in logins:
            details.append(f"{who} → GitHub {logins[who]}")
        else:
            status = FAILING
            details.append(f"{who} has no GitHub login mapping")
        if _jira_key(ctx):
            if who in accounts:
                details.append(f"{who} → Jira account mapped")
            else:
                status = _worst([status, WARNING])
                details.append(f"{who} has no Jira account mapping (Jira's user search is the fallback)")
    else:
        status = WARNING
        details.append("The requester has no email to check; check each approver is mapped.")
    details.append(f"{len(logins)} GitHub login mapping(s), {len(accounts)} Jira account mapping(s) in total")
    summary = {
        OK: "You are mapped; approvals and assignments resolve to your accounts.",
        WARNING: "GitHub logins are mapped, but some mappings are missing.",
        FAILING: "Your email has no GitHub login mapping; your PR approvals will not count.",
    }[status]
    return _item("identity", title, status, summary, details=details,
                 fix="Map each approver in System Settings → Identity (GitHub logins and Jira account ids).", fix_link=LINK_IDENTITY)


# ---------------------------------------------------------------------------
# 8. Project settings
# ---------------------------------------------------------------------------


def check_project_settings(ctx: ChecklistContext) -> dict[str, Any]:
    title = "Project settings"
    p = ctx.project
    problems: list[str] = []
    autonomy = str(p.get("revision_autonomy") or "")
    if autonomy not in REVISION_AUTONOMY:
        problems.append(f"revision_autonomy is {autonomy or 'unset'} (one of {', '.join(REVISION_AUTONOMY)})")
    bdd = str(p.get("bdd_mode") or "")
    if bdd not in BDD_MODES:
        problems.append(f"bdd_mode is {bdd or 'unset'} (one of {', '.join(BDD_MODES)})")
    try:
        parallel = int(p.get("max_parallel_tickets"))
    except (TypeError, ValueError):
        parallel = 0
    low, high = MAX_PARALLEL_RANGE
    if not low <= parallel <= high:
        problems.append(f"max_parallel_tickets is {p.get('max_parallel_tickets')!r} (a whole number from {low} to {high})")
    if problems:
        return _item("project_settings", title, FAILING, "Some delivery settings are missing or invalid.", details=problems,
                     fix="Set them in Delivery, Testing and Revisions on this page.", fix_link=project_link(p.get("id", "")))
    return _item("project_settings", title, OK,
                 f"Revisions {autonomy}, up to {parallel} ticket(s) in parallel, BDD {bdd}.")


CHECKS: list[tuple[str, str, Callable[[ChecklistContext], dict[str, Any]]]] = [
    ("jira_allowed", "Jira project allowed", check_jira_allowed),
    ("jira_project", "Jira reachable", check_jira_project),
    ("repositories", "Repositories linked and reachable", check_repositories),
    ("repo_allowed", "Repositories allowed", check_repo_allowed),
    ("branch_protection", "Branch protection requires harness checks", check_branch_protection),
    ("ci_on_pull_requests", "CI on pull requests", check_ci),
    ("identity", "Approvers mapped to GitHub and Jira", check_identity),
    ("project_settings", "Project settings", check_project_settings),
]


def run_checklist(
    ctx: ChecklistContext,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    checks: list[tuple[str, str, Callable[[ChecklistContext], dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """Run every check concurrently; each gets ``timeout`` seconds and its errors stay in its item."""
    checks = checks or CHECKS
    pool = ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="project-checklist")
    try:
        futures = [(id, title, pool.submit(fn, ctx)) for id, title, fn in checks]
        # The checks run side by side, so one deadline bounds the whole checklist.
        deadline = time.monotonic() + timeout
        items = []
        for id, title, future in futures:
            try:
                items.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
            except FutureTimeout:
                items.append(_item(id, title, WARNING, f"Could not check: timed out after {timeout:g}s.",
                                   fix="Re-check; if it keeps timing out, the service it calls is slow or unreachable."))
            except Exception as exc:  # noqa: BLE001
                items.append(_item(id, title, WARNING, f"Could not check: {str(exc)[:200] or type(exc).__name__}",
                                   fix="Re-check; if it keeps failing, see the gateway log."))
    finally:
        # A check still waiting on a slow call is abandoned, not awaited.
        pool.shutdown(wait=False, cancel_futures=True)
    ready = sum(1 for item in items if item["status"] == OK)
    return {
        "project_id": ctx.project.get("id"),
        "items": items,
        "ready": ready,
        "total": len(items),
        "summary": f"{ready} of {len(items)} ready",
    }
