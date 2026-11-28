"""Self-fix PRs: the harness delivers a fix for its own filed bug, as a draft PR a person merges.

For a filed harness issue (:mod:`ts_harness.improvement.harness_loop`):

* **Fixable** means: the failure points at harness code (``kind == "code"``) and at least one
  harness file; configuration, access and operations problems are labelled ``needs-human``.
* **Off limits**: every file the fix is expected to touch (the files the failure points at) is
  checked with :func:`ts_harness.improvement.off_limits.is_off_limits`. Any hit: no fix, the issue
  is labelled ``needs-human`` with the reason. After the PR opens, its actual files are checked
  again; an off-limits file labels the PR ``needs-human`` and says so on it.
* **Delivery**: a ``deliver_work_order`` run against the harness repository (``TS_HARNESS_SELF_REPO``)
  into its development branch (``TS_HARNESS_SELF_REPO_BRANCH``, default ``dev``), with a work order
  built from the sanitised issue. The normal flow runs: coding, CI, code review, QA and the human
  merge wait. The work order requires a regression test.
* **The PR** is made a draft, labelled ``harness-self-fix`` and linked to the issue. A revision run
  marks a PR ready when it ends, so the PR is made a draft again whenever its head moves. Nothing
  here merges, approves or enables auto-merge.
* **Limits**: at most :data:`MAX_OPEN_FIXES` self-fix PRs open at a time, :data:`MAX_FIXES_PER_WEEK`
  started in any 7 days, one per issue (ever), and one started per run.
* **Budget**: a fix starts only when the improvement budget can cover :data:`SELF_FIX_ESTIMATE_USD`;
  its coding jobs' cost is charged to the budget (purpose ``harness_self_fix``) as it accrues.
  When the budget is used up, fixes wait quietly for next month.
* **Fail safe**: when the harness repository is not deliverable in this install (not on the repo
  allowlist nor linked to a project, no GitHub credentials), no fix starts and the status says
  what an admin must configure.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable

from ts_harness.improvement import sanitize as sz
from ts_harness.improvement.off_limits import is_off_limits

logger = logging.getLogger(__name__)

MAX_OPEN_FIXES = 2
MAX_FIXES_PER_WEEK = 3
SELF_FIX_ESTIMATE_USD = 20.0
FIX_LABEL = "harness-self-fix"
WORKFLOW_TYPE = "deliver_work_order"
BUDGET_PURPOSE = "harness_self_fix"
WORK_ORDER_PREFIX = "HSF"
TERMINAL = frozenset({"succeeded", "completed", "failed", "cancelled", "canceled"})


def _owner_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def deliverability(settings: Any, *, memory: Any, tenant_id: str, config: Any,
                   token: Callable[[str], str] | None = None) -> list[str]:
    """What an admin must configure before the harness can deliver to its own repository (empty: ready)."""
    from ts_harness.config.bindings import check_repo_allowed_or_linked, tenant_bindings

    problems: list[str] = []
    defaults = getattr(config, "program_defaults", None)
    error = check_repo_allowed_or_linked(settings.repo_url, tenant_bindings(defaults if isinstance(defaults, dict) else {}),
                                         tenant_id=tenant_id, memory=memory)
    if error:
        problems.append(
            f"Self-fixes are waiting: {error}. Add {settings.repo} to the repo allowlist "
            "(program_defaults.repo_picker.repos) or link it to a project in System → Projects."
        )
    if token is None:
        from ts_harness.integrations.github_client import repo_token as token
    try:
        has_token = bool(token(settings.repo_url))
    except Exception:  # noqa: BLE001
        has_token = False
    if not has_token:
        problems.append(
            f"Self-fixes are waiting: no GitHub credentials for {settings.repo}. Install the GitHub App on it "
            "(contents, pull requests and issues: read and write) or set GITHUB_TOKEN."
        )
    try:
        from ts_harness.workflows.hooks.fork_subworkflow import load_skeleton

        load_skeleton(WORKFLOW_TYPE)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Self-fixes are waiting: the {WORKFLOW_TYPE} workflow is not available ({str(exc)[:120]}).")
    return problems


def expected_files(row: dict[str, Any]) -> list[str]:
    """The harness files the fix is expected to touch: the files the failure points at."""
    files = [str(f) for f in row.get("files") or [] if str(f or "").strip()]
    component = str(row.get("component") or "")
    if component and "/" in component and component not in files:
        files.append(component)
    return files


def eligibility(row: dict[str, Any]) -> tuple[str, str]:
    """``("ok", "")``, or the fix status to record (``not_fixable``, ``needs_human``) and why."""
    kind = str(row.get("kind") or "unknown")
    if kind == "configuration":
        return "not_fixable", "a configuration or access problem: an admin must fix the setup, not the code"
    if kind == "operations":
        return "not_fixable", "an operations problem (workers, services): an operator must look at the hosts"
    files = expected_files(row)
    if kind != "code" or not files:
        return "not_fixable", "no harness file identified: a person must diagnose it first"
    for path in files:
        hit = is_off_limits(path=path)
        if hit:
            return "needs_human", f"the fix would touch {path}, which is off limits ({hit['why']})"
    return "ok", ""


def work_order_id(row: dict[str, Any]) -> str:
    return f"{WORK_ORDER_PREFIX}-{int(row.get('issue_number') or 0)}"


def work_order_body(row: dict[str, Any], settings: Any) -> str:
    """The self-fix work order the coding agent, code review and QA read: sanitised issue facts only."""
    files = expected_files(row)
    listed = "\n".join(f"- `{f}`" for f in files)
    stack = f"\nHarness stack:\n```\n{row.get('stack')}\n```\n" if row.get("stack") else ""
    return f"""# {work_order_id(row)} Fix harness bug: {row.get('title')}
Complexity: Low

## Summary
TS Harness filed {row.get('issue_url') or 'an issue'} against itself. Signature: {row.get('signature') or '(none)'}.
Component: {row.get('component')}. Seen {int(row.get('occurrences') or 0)} time(s).
Diagnosis: {row.get('diagnosis') or '(none recorded)'}
{stack}
## Requirements
- Find the root cause in the files below and fix it there.
- Add a regression test under `tests/` that fails without the fix and passes with it. A fix without
  a regression test is not done.
- Do not change any file, setting or constant on the off-limits list
  (`runtime/ts_harness/improvement/off_limits.py`): security rules, gate blocking rules,
  permissions, budgets, the arbiter's limits, the metrics and guard, and the self-improvement loop
  itself. If the fix needs one of them, stop and say so in the run summary instead.
- Do not weaken, skip or delete existing tests or checks.
- `python3 -m pytest -q tests` and `ruff check runtime scripts tests` pass.

## In Scope
{listed}
- New or updated tests under `tests/`.

## Out of Scope
- Any off-limits file, setting or constant.
- Refactors, renames and changes unrelated to this failure.
- Client data: the issue holds harness-side facts only; never add client repository names, tickets or text.

## Acceptance Criteria
- A regression test reproduces the failure (it fails before the fix) and passes after it.
- The full backend test suite and ruff pass.
- No off-limits file changed; the PR targets `{settings.branch}` and is left for a person to merge.
"""


def _issue_comment(github: Any, settings: Any, number: int, text: str) -> None:
    owner, name = _owner_repo(settings.repo)
    github.comment(owner, name, number, text)


def _label(github: Any, settings: Any, number: int, labels: list[str]) -> None:
    owner, name = _owner_repo(settings.repo)
    github.request("POST", f"/repos/{owner}/{name}/issues/{number}/labels", body={"labels": labels})


def _default_fork(memory: Any, *, tenant_id: str, intake: dict[str, Any], actor: str) -> str:
    from ts_harness.workflows.hooks.fork_subworkflow import fork_child

    return fork_child(memory, parent_workflow_id="", tenant_id=tenant_id, child_type=WORKFLOW_TYPE,
                      intake_input=intake, actor=actor)


def _default_make_draft(pr_url: str) -> bool:
    from ts_harness.implementation.repo import set_pull_request_draft
    from ts_harness.integrations.github_client import repo_token

    env = os.environ.copy()
    token = repo_token(pr_url)
    if token:
        env["GH_TOKEN"] = token
    return set_pull_request_draft(pr_url, True, env=env)


def mark_needs_human(memory: Any, github: Any, settings: Any, row: dict[str, Any], status: str, reason: str) -> None:
    from ts_harness.improvement.harness_loop import NEEDS_HUMAN_LABEL

    number = int(row.get("issue_number") or 0)
    if number:
        try:
            _label(github, settings, number, [NEEDS_HUMAN_LABEL])
            _issue_comment(github, settings, number, f"No self-fix: {reason}. A person needs to take this one.")
        except Exception as exc:  # noqa: BLE001 - the status below still records it
            logger.info("needs-human label not set on #%s: %s", number, exc)
    update = memory.update_harness_issue(str(row["id"]), fix_status=status, fix_reason=reason[:600])
    row.update((update or {}).get("issue") or {})


def start_fix(memory: Any, github: Any, settings: Any, row: dict[str, Any], *, tenant_id: str,
              fork: Callable[..., str]) -> str:
    """Start the self-fix delivery; returns its workflow id. Claims the issue's one fix first."""
    from ts_harness.improvement.harness_loop import ACTOR

    claimed = memory.update_harness_issue(str(row["id"]), fix_status="started", fix_started=True,
                                          fix_reason="self-fix delivery starting")
    row.update((claimed or {}).get("issue") or {})
    intake = {
        "work_order_id": work_order_id(row),
        "work_order_ref": work_order_id(row),
        "work_order_body": work_order_body(row, settings),
        "repo_url": settings.repo_url,
        "base_branch": settings.branch,
        "summary": f"Self-fix: {row.get('title')}"[:200],
    }
    try:
        workflow_id = fork(memory, tenant_id=tenant_id, intake=intake, actor=ACTOR)
    except Exception as exc:
        update = memory.update_harness_issue(str(row["id"]), fix_status="failed",
                                             fix_reason=f"the delivery could not start: {type(exc).__name__}"[:600])
        row.update((update or {}).get("issue") or {})
        raise
    update = memory.update_harness_issue(str(row["id"]), fix_workflow_id=workflow_id,
                                         fix_reason="self-fix delivery running (coding, CI, review, QA)")
    row.update((update or {}).get("issue") or {})
    try:
        _issue_comment(github, settings, int(row["issue_number"]),
                       f"Self-fix started (internal workflow `{workflow_id}`). It opens a draft PR into "
                       f"`{settings.branch}` labelled `{FIX_LABEL}`; a person reviews and merges it.")
    except Exception as exc:  # noqa: BLE001
        logger.info("self-fix start not noted on the issue: %s", exc)
    return workflow_id


def _pr_url(payload: dict[str, Any]) -> str:
    tasks = sorted(payload.get("tasks") or [], key=lambda t: str(t.get("updated_at") or ""), reverse=True)
    for task in tasks:
        output = task.get("output") if isinstance(task.get("output"), dict) else {}
        if task.get("step_name") == WORKFLOW_TYPE and str(output.get("pr_url") or "").startswith("https://"):
            return str(output["pr_url"])
    return ""


def _job_cost(job: dict[str, Any]) -> float:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    usage = result.get("usage") or []
    total = sum(float(row.get("cost_usd") or 0) for row in usage if isinstance(row, dict))
    return total or float(job.get("cost_usd") or 0)


def charge_cost(memory: Any, budget: Any, row: dict[str, Any]) -> float:
    """Charge the fix's coding cost accrued since the last charge; returns the amount charged."""
    workflow_id = str(row.get("fix_workflow_id") or "")
    if not workflow_id:
        return 0.0
    jobs = memory.list_coding_jobs(workflow_id=workflow_id, limit=100) or []
    total = round(sum(_job_cost(job) for job in jobs), 4)
    delta = round(total - float(row.get("fix_charged_usd") or 0), 4)
    if delta <= 0:
        return 0.0
    budget.charge(delta, BUDGET_PURPOSE, ref=str(row.get("fingerprint") or ""))
    update = memory.update_harness_issue(str(row["id"]), charge_usd=delta)
    row.update((update or {}).get("issue") or {})
    return delta


def track_fix(memory: Any, github: Any, settings: Any, row: dict[str, Any], *,
              make_draft: Callable[[str], bool]) -> str:
    """Follow a started fix: draft, label and link its PR; record merged, closed or failed."""
    workflow_id = str(row.get("fix_workflow_id") or "")
    if not workflow_id:
        return "no workflow"
    payload = memory.get_workflow(workflow_id) or {}
    workflow = payload.get("workflow") or {}
    pr_url = _pr_url(payload) or str(row.get("fix_pr_url") or "")
    if not pr_url:
        if str(workflow.get("status") or "") in TERMINAL:
            update = memory.update_harness_issue(str(row["id"]), fix_status="failed",
                                                 fix_reason=f"the delivery ended ({workflow.get('status')}) without a PR")
            row.update((update or {}).get("issue") or {})
            return "failed"
        return "running"
    from ts_harness.integrations.github_client import parse_github_pr_url

    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        return "unknown PR"
    owner, name, number = parsed
    if f"{owner}/{name}".lower() != settings.repo.lower():
        update = memory.update_harness_issue(str(row["id"]), fix_status="needs_human",
                                             fix_reason="the delivery opened a PR outside the harness repository")
        row.update((update or {}).get("issue") or {})
        return "wrong repository"
    pull = github.get_pull(owner, name, number) or {}
    changes: dict[str, Any] = {}
    if pr_url != row.get("fix_pr_url"):
        changes.update(fix_pr_url=pr_url, fix_status="pr_open", fix_reason="draft PR open; a person merges it")
        try:
            _label(github, settings, number, [FIX_LABEL])
            github.comment(owner, name, number,
                           f"Self-fix for {row.get('issue_url') or 'a harness issue'}, opened by TS Harness about itself. "
                           "It is never merged automatically: review it, mark it ready and merge it yourself.")
            _issue_comment(github, settings, int(row["issue_number"]), f"Self-fix PR: {pr_url} (draft).")
        except Exception as exc:  # noqa: BLE001
            logger.info("self-fix PR %s not labelled or linked: %s", pr_url, exc)
    head = str((pull.get("head") or {}).get("sha") or "")
    if pull.get("merged") or pull.get("merged_at"):
        changes.update(fix_status="merged", fix_reason="merged by a person")
    elif str(pull.get("state") or "") == "closed":
        changes.update(fix_status="closed", fix_reason="closed without merging")
    else:
        if head and head != row.get("fix_pr_head") and not pull.get("draft"):
            # New commits from the harness (a revision marks the PR ready when it ends): draft again.
            if make_draft(pr_url):
                changes["fix_pr_head"] = head
        elif head and head != row.get("fix_pr_head"):
            changes["fix_pr_head"] = head
        if "off limits" not in str(row.get("fix_reason") or ""):
            touched = [str(f.get("filename") or "") for f in github.list_pull_files(owner, name, number) or []]
            hits = [(path, is_off_limits(path=path)) for path in touched]
            hits = [(path, hit) for path, hit in hits if hit]
            if hits:
                from ts_harness.improvement.harness_loop import NEEDS_HUMAN_LABEL

                reason = f"the PR changes {hits[0][0]}, which is off limits ({hits[0][1]['why']})"
                changes.update(fix_reason=reason[:600])
                try:
                    _label(github, settings, number, [NEEDS_HUMAN_LABEL])
                    github.comment(owner, name, number, f"Stop: {reason}. Do not merge without the owner's review.")
                except Exception as exc:  # noqa: BLE001
                    logger.info("off-limits warning not posted on %s: %s", pr_url, exc)
    if changes:
        update = memory.update_harness_issue(str(row["id"]), **changes)
        row.update((update or {}).get("issue") or {})
    return str(row.get("fix_status") or "")


def run(memory: Any, github: Any, settings: Any, ctx: sz.ClientContext, *, tenant_id: str, rows: list[dict[str, Any]],
        listing: dict[str, Any], now: datetime, config: Any, budget: Any = None, fork: Callable[..., str] | None = None,
        make_draft: Callable[[str], bool] | None = None, token: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Track running fixes, then start at most one new fix if the limits, the budget and the setup allow."""
    out: dict[str, Any] = {"tracked": {}, "started": [], "skipped": {}, "problems": [], "charged_usd": 0.0}
    if not settings.fixing:
        out["status"] = "off"
        return out
    if budget is None:
        from ts_harness.improvement.budget import ImprovementBudget

        budget = ImprovementBudget(tenant_id=tenant_id, memory=memory)
    fork = fork or _default_fork
    make_draft = make_draft or _default_make_draft

    for row in rows:
        if row.get("fix_status") not in {"started", "pr_open"}:
            continue
        try:
            out["tracked"][str(row.get("issue_number"))] = track_fix(memory, github, settings, row, make_draft=make_draft)
            out["charged_usd"] += charge_cost(memory, budget, row)
        except Exception as exc:  # noqa: BLE001 - tried again next run
            logger.warning("self-fix for %s not tracked: %s", row.get("fingerprint"), exc)

    candidates = [r for r in rows if r.get("issue_number") and str(r.get("issue_state") or "open") == "open"
                  and r.get("fix_status") == "none" and not r.get("fix_started_at")]
    if not candidates:
        out["status"] = "idle"
        return out
    open_fixes = sum(1 for r in rows if r.get("fix_status") in {"started", "pr_open"})
    open_fixes = max(open_fixes, int(listing.get("open_fixes") or 0))
    week = int(listing.get("fixes_last_7_days") or 0)
    problems: list[str] | None = None
    for row in candidates:
        number = str(row.get("issue_number"))
        verdict, reason = eligibility(row)
        if verdict != "ok":
            try:
                mark_needs_human(memory, github, settings, row, verdict, reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("issue #%s not marked: %s", number, exc)
            out["skipped"][number] = reason
            continue
        if out["started"]:
            out["skipped"][number] = "one self-fix starts per run"
            continue
        if open_fixes >= MAX_OPEN_FIXES:
            out["skipped"][number] = f"{open_fixes} self-fix PRs are open (limit {MAX_OPEN_FIXES})"
            continue
        if week >= MAX_FIXES_PER_WEEK:
            out["skipped"][number] = f"{week} self-fixes started in the last 7 days (limit {MAX_FIXES_PER_WEEK})"
            continue
        if problems is None:
            problems = deliverability(settings, memory=memory, tenant_id=tenant_id, config=config, token=token)
            out["problems"].extend(problems)
        if problems:
            out["skipped"][number] = "the harness repository is not deliverable in this install"
            continue
        if not budget.can_spend(SELF_FIX_ESTIMATE_USD):
            out["skipped"][number] = "the improvement budget is used up for this month"
            out["status"] = "paused"
            continue
        try:
            workflow_id = start_fix(memory, github, settings, row, tenant_id=tenant_id, fork=fork)
        except Exception as exc:  # noqa: BLE001 - recorded on the row; never retried for this issue
            logger.warning("self-fix for #%s not started: %s", number, exc)
            out["skipped"][number] = f"could not start: {type(exc).__name__}"
            continue
        out["started"].append({"issue_number": row.get("issue_number"), "workflow_id": workflow_id})
        open_fixes += 1
        week += 1
    out.setdefault("status", "ok")
    out["charged_usd"] = round(out["charged_usd"], 4)
    return out
