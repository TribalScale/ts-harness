"""Automatic canaries for proposals that passed an offline check, with a stop rule and rollback.

**Start** (:func:`start`): the loop moves a proposal that passed its replay to ``canary`` on its
own. Only one proposal per target is live (``canary`` or ``awaiting_approval``) at a time; the
memory API enforces it with a unique index.

**Assignment** (:func:`assign_for_task`): when a ticket's workflow starts (the first step the
runner dispatches for it), each running canary assigns it to ``candidate`` or ``control`` of
variant ``proposal-<id>`` with ``set_ticket_variant``: a stable hash of the workflow id and the
proposal id against the canary's share (``TS_HARNESS_CANARY_SHARE``, default 0.5). Workflows that
started before the canary are left out. Only workflows that run a coding step take part
(``CANARY_WORKFLOW_TYPES``).

**Behaviour** (:func:`apply_to_spec`): a coding job of a ``candidate`` ticket uses the candidate
rubric for its pre-push review (the review skill with the proposal's diff applied, the sections
the reviewer reads). Control tickets, and every ticket once the proposal is rolled back, use the
current rubric.

**Stop rule** (:func:`check`, from the hourly improvement job), comparing ``delivery_metrics``
grouped by ``variants.proposal-<id>``:

* any candidate ticket with a problem after merge -> roll back at once;
* the candidate diff no longer applies to the review skill (it changed) -> roll back;
* once ``MIN_TICKETS`` (8) candidate and 8 control tickets have finished: candidate autonomy
  more than ``AUTONOMY_MARGIN`` (12.5 points: one ticket in eight) below control, or a higher
  post-merge problem rate -> roll back; otherwise -> ``awaiting_approval`` (Inbox);
* not enough tickets after ``MAX_DAYS`` (45) -> roll back.

**Promotion** (:func:`promote`, a person in the Inbox): a ``skill_edit`` or ``fragment_change``
opens a pull request against the harness repository (``TS_HARNESS_IMPROVEMENT_PR_REPO``, base
``TS_HARNESS_IMPROVEMENT_PR_BASE``, default ``dev``) when set and reachable; the harness never
merges it. Otherwise, and for setting changes, the diff or the new value is shown for a person to
apply, and the proposal is recorded as promoted by hand.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ts_harness.improvement import patches
from ts_harness.improvement import proposals as pp

logger = logging.getLogger(__name__)

CANDIDATE = "candidate"
CONTROL = "control"
DEFAULT_SHARE = 0.5
MIN_TICKETS = 8
AUTONOMY_MARGIN = 0.125
GUARD_MARGIN = 0.0
MAX_DAYS = 45
CANARY_WORKFLOW_TYPES = frozenset({"implement_jira_ticket", "deliver_work_order"})
_CACHE_SECONDS = 60
_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}
_pre_existing: dict[str, float] = {}


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def share() -> float:
    """``TS_HARNESS_CANARY_SHARE``: the share of new tickets that get the candidate (0.05-0.95)."""
    try:
        value = float(os.environ.get("TS_HARNESS_CANARY_SHARE") or DEFAULT_SHARE)
    except ValueError:
        return DEFAULT_SHARE
    return min(0.95, max(0.05, value))


def enabled() -> bool:
    """``TS_HARNESS_CANARIES`` (default on). Off: no new canary starts and no ticket is assigned;
    tickets already assigned keep their variant until the proposal is rolled back."""
    return (os.environ.get("TS_HARNESS_CANARIES") or "on").strip().lower() not in {"0", "off", "false", "no"}


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _pre_existing.clear()


def live(memory: Any, *, tenant_id: str = "default") -> list[dict[str, Any]]:
    """Proposals in ``canary`` or ``awaiting_approval`` (cached for a minute in this process)."""
    key = f"live:{tenant_id}"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return list(hit[1])
    try:
        rows = memory.list_improvement_proposals(tenant_id=tenant_id, status="canary,awaiting_approval", limit=50)
    except Exception as exc:  # noqa: BLE001 - no canary is better than a failed dispatch
        logger.info("live canaries unavailable: %s", exc)
        rows = []
    with _lock:
        _cache[key] = (now, rows)
    return list(rows)


def bucket(proposal_id: Any, workflow_id: str, candidate_share: float) -> str:
    digest = hashlib.sha256(f"{proposal_id}:{workflow_id}".encode()).digest()
    return CANDIDATE if int.from_bytes(digest[:4], "big") / 2**32 < candidate_share else CONTROL


def _workflow_started(memory: Any, workflow_id: str) -> datetime | None:
    try:
        record = memory.get_workflow(workflow_id) or {}
    except Exception:  # noqa: BLE001
        return None
    workflow = record.get("workflow") if isinstance(record.get("workflow"), dict) else record
    return _parse(workflow.get("created_at"))


def assign_for_task(task: dict[str, Any], *, memory: Any, tenant_id: str | None = None) -> dict[str, str]:
    """Assign the task's ticket to each running canary it is not in yet; returns what was set."""
    from ts_harness.improvement.versions import set_ticket_variant, ticket_variants

    if not enabled() or str(task.get("workflow_type") or "") not in CANARY_WORKFLOW_TYPES:
        return {}
    workflow_id = str(task.get("workflow_id") or "").strip()
    if not workflow_id:
        return {}
    running = [p for p in live(memory, tenant_id=tenant_id or str(task.get("tenant_id") or "default"))
               if p.get("status") == "canary"]
    if not running:
        return {}
    current = ticket_variants(workflow_id, memory=memory)
    assigned: dict[str, str] = {}
    started: datetime | None = None
    for proposal in running:
        name = pp.variant_name(proposal)
        if name in current or f"{workflow_id}:{name}" in _pre_existing:
            continue
        canary = proposal.get("canary") if isinstance(proposal.get("canary"), dict) else {}
        since = _parse(canary.get("started_at"))
        if started is None:
            started = _workflow_started(memory, workflow_id)
        if since is None or started is None or started < since:
            with _lock:
                if len(_pre_existing) > 5000:
                    _pre_existing.clear()
                _pre_existing[f"{workflow_id}:{name}"] = time.monotonic()
            continue
        value = bucket(proposal["id"], workflow_id, float(canary.get("share") or DEFAULT_SHARE))
        try:
            set_ticket_variant(workflow_id, name, value, memory=memory)
            assigned[name] = value
        except Exception as exc:  # noqa: BLE001 - the ticket runs as control, unassigned
            logger.warning("canary assignment failed for %s: %s", workflow_id, exc)
    return assigned


def candidate_rubric(proposal: dict[str, Any], *, reader: Any = None) -> str | None:
    """The pre-push rubric a candidate ticket reviews with, or None when the diff no longer applies."""
    from ts_harness.improvement.replay import rubrics

    try:
        return rubrics(proposal, reader=reader)[1] or None
    except patches.PatchError as exc:
        logger.warning("proposal %s no longer applies: %s", proposal.get("id"), exc)
        return None


def apply_to_spec(spec: dict[str, Any], task_input: dict[str, Any], *, memory: Any,
                  tenant_id: str = "default", reader: Any = None) -> dict[str, Any]:
    """The coding job spec as the ticket's canary variant wants it (unchanged for control)."""
    from ts_harness.improvement.versions import VERSIONS_KEY

    versions = task_input.get(VERSIONS_KEY) if isinstance(task_input.get(VERSIONS_KEY), dict) else {}
    variants = versions.get("variants") if isinstance(versions.get("variants"), dict) else {}
    chosen = [name for name, value in variants.items() if str(name).startswith("proposal-") and value == CANDIDATE]
    if not chosen or not spec.get("review_rubric"):
        return spec
    by_name = {pp.variant_name(p): p for p in live(memory, tenant_id=tenant_id)}
    for name in chosen:
        proposal = by_name.get(name)
        if proposal is None or proposal.get("target") != pp.PRE_PUSH_TARGET:
            continue
        rubric = candidate_rubric(proposal, reader=reader)
        if rubric:
            return {**spec, "review_rubric": rubric}
    return spec


def start(memory: Any, proposal: dict[str, Any], *, tenant_id: str = "default",
          now: datetime | None = None) -> dict[str, Any]:
    """Move an ``evaluating`` proposal that passed its replay into ``canary``. When the target
    already has a live proposal, it goes back to ``proposed`` and waits for the slot."""
    from ts_harness.memory.client import MemoryApiConflict

    stamp = _iso(_now(now))
    canary = {"started_at": stamp, "share": share(), "min_tickets": MIN_TICKETS, "variant": pp.variant_name(proposal),
              "autonomy_margin": AUTONOMY_MARGIN, "max_days": MAX_DAYS}
    busy = [p for p in memory.list_improvement_proposals(tenant_id=tenant_id, status="canary,awaiting_approval",
                                                         target=str(proposal.get("target") or ""), limit=5)
            if p.get("id") != proposal.get("id")]
    if busy or not enabled():
        reason = (f"passed offline; waiting for proposal {busy[0]['id']} on the same target" if busy
                  else "passed offline; canaries are off (TS_HARNESS_CANARIES)")
        return pp.move(memory, proposal, "proposed", reason=reason, tenant_id=tenant_id)
    try:
        moved = pp.move(memory, proposal, "canary", reason="passed the offline replay check", tenant_id=tenant_id,
                        canary=canary)
    except MemoryApiConflict:
        return pp.move(memory, memory.get_improvement_proposal(proposal["id"], tenant_id=tenant_id), "proposed",
                       reason="passed offline; another proposal took the target first", tenant_id=tenant_id)
    clear_cache()
    return moved


def _side(groups: list[dict[str, Any]], value: str) -> dict[str, Any]:
    group = next((g for g in groups if g.get("value") == value), None)
    summary = (group or {}).get("summary") or {}
    autonomy = summary.get("autonomy") or {}
    guard = summary.get("guard") or {}
    return {
        "tickets": int((group or {}).get("tickets") or 0),
        "finished": int(summary.get("finished") or 0),
        "merged": int(summary.get("merged") or 0),
        "autonomy_rate": autonomy.get("rate"),
        "problem_rate": guard.get("problem_rate"),
        "tickets_with_problems": int(guard.get("tickets_with_problems") or 0),
        "guard_fully_checked": int(guard.get("fully_checked") or 0),
        "cost_median_usd": (summary.get("cost_usd") or {}).get("median"),
        "time_to_merge_median_seconds": (summary.get("time_to_merge_seconds") or {}).get("median"),
    }


def verdict(candidate: dict[str, Any], control: dict[str, Any], *, age_days: float,
            min_tickets: int = MIN_TICKETS) -> tuple[str, str]:
    """(``rollback`` | ``approve`` | ``wait``, reason) by the stop rule in the module docstring."""
    if candidate["tickets_with_problems"] > 0:
        return "rollback", f"{candidate['tickets_with_problems']} candidate ticket(s) had a problem after merge"
    if candidate["finished"] < min_tickets or control["finished"] < min_tickets:
        if age_days > MAX_DAYS:
            return "rollback", (f"not enough tickets in {MAX_DAYS} days ({candidate['finished']} candidate, "
                                f"{control['finished']} control finished)")
        return "wait", f"{candidate['finished']}/{min_tickets} candidate and {control['finished']}/{min_tickets} control finished"
    cand, ctrl = candidate["autonomy_rate"] or 0.0, control["autonomy_rate"] or 0.0
    if cand < ctrl - AUTONOMY_MARGIN - 1e-9:
        return "rollback", f"autonomy {cand:.0%} vs control {ctrl:.0%}: worse by more than {AUTONOMY_MARGIN:.1%}"
    cand_p, ctrl_p = candidate["problem_rate"], control["problem_rate"]
    if cand_p is not None and ctrl_p is not None and cand_p > ctrl_p + GUARD_MARGIN + 1e-9:
        return "rollback", f"problems after merge {cand_p:.0%} vs control {ctrl_p:.0%}"
    return "approve", f"autonomy {cand:.0%} vs control {ctrl:.0%}; no candidate problem after merge"


def rollback(memory: Any, proposal: dict[str, Any], reason: str, *, actor: str = pp.ACTOR,
             tenant_id: str = "default", **fields: Any) -> dict[str, Any]:
    moved = pp.move(memory, proposal, "rolled_back", actor=actor, reason=reason, tenant_id=tenant_id, **fields)
    clear_cache()
    logger.info("canary for proposal %s rolled back: %s", proposal.get("id"), reason)
    return moved


def check(memory: Any, *, tenant_id: str = "default", now: datetime | None = None,
          metrics: Callable[..., dict[str, Any]] | None = None, reader: Any = None) -> dict[str, Any]:
    """Apply the stop rule to every live proposal; returns what happened to each."""
    if metrics is None:
        from ts_harness.workflows.delivery_metrics import delivery_metrics as metrics
    moment = _now(now)
    results = []
    for proposal in memory.list_improvement_proposals(tenant_id=tenant_id, status="canary,awaiting_approval", limit=50):
        canary = dict(proposal.get("canary") or {})
        started = _parse(canary.get("started_at")) or moment
        age = max(0.0, (moment - started).total_seconds() / 86400)
        name = pp.variant_name(proposal)
        try:
            data = metrics(memory, tenant_id=tenant_id, days=max(1, min(90, int(age) + 2)), group_by=f"variants.{name}",
                           guard=True, include_running=False, now=moment)
        except Exception as exc:  # noqa: BLE001 - next run tries again
            results.append({"id": proposal["id"], "action": "error", "reason": str(exc)[:200]})
            continue
        groups = list(data.get("groups") or [])
        candidate, control = _side(groups, CANDIDATE), _side(groups, CONTROL)
        canary["latest"] = {"checked_at": _iso(moment), "candidate": candidate, "control": control}
        action, reason = verdict(candidate, control, age_days=age, min_tickets=int(canary.get("min_tickets") or MIN_TICKETS))
        if proposal.get("target") == pp.PRE_PUSH_TARGET and candidate_rubric(proposal, reader=reader) is None:
            action, reason = "rollback", "the review skill changed and the candidate no longer applies"
        canary["latest"]["decision"] = {"action": action, "reason": reason}
        if action == "approve" and proposal.get("status") != "canary":
            action = "wait"  # already waiting for a person; the guard keeps being watched
        try:
            if action == "rollback":
                rollback(memory, proposal, reason, tenant_id=tenant_id, canary=canary)
            elif action == "approve":
                pp.move(memory, proposal, "awaiting_approval", reason=reason, tenant_id=tenant_id, canary=canary)
                clear_cache()
            else:
                pp.annotate(memory, proposal, tenant_id=tenant_id, canary=canary)
        except Exception as exc:  # noqa: BLE001 - moved by a person meanwhile
            results.append({"id": proposal["id"], "action": "conflict", "reason": str(exc)[:200]})
            continue
        results.append({"id": proposal["id"], "action": action, "reason": reason})
    return {"checked": len(results), "results": results}


# Promotion


def pr_target() -> tuple[str, str, str] | None:
    """(owner, repo, base) from ``TS_HARNESS_IMPROVEMENT_PR_REPO`` / ``..._BASE``; None when unset."""
    repo = os.environ.get("TS_HARNESS_IMPROVEMENT_PR_REPO", "").strip()
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return owner, name, os.environ.get("TS_HARNESS_IMPROVEMENT_PR_BASE", "").strip() or "dev"


PR_BODY = """An improvement proposal from the harness's self-improvement loop, approved by {actor}.

**{title}**

{summary}

- Type: {type}; target: {target}
- Metric it should move: {metric}
- Expected effect: {expected_effect}
- Risk: {risk}
- Offline check: {offline}
- Canary: {canary}

The harness opened this pull request and will not merge it. Review it like any other change
(a skill edit may need its `version` raised).
"""


def open_pr(proposal: dict[str, Any], *, actor: str, github: Any = None) -> dict[str, Any]:
    """Open a pull request with the proposal's diff on a new branch; never merges."""
    target = pr_target()
    if target is None:
        raise RuntimeError("TS_HARNESS_IMPROVEMENT_PR_REPO is not set")
    owner, repo, base = target
    if github is None:
        from ts_harness.integrations.github_client import GitHubClient

        github = GitHubClient.from_env()
    diff = str((proposal.get("change") or {}).get("diff") or "")
    ref = github.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{base}")
    base_sha = str(((ref or {}).get("object") or {}).get("sha") or "")
    if not base_sha:
        raise RuntimeError(f"cannot read {owner}/{repo}@{base}")
    files: dict[str, tuple[str, str]] = {}

    def remote(path: str) -> str:
        payload = github.get_file(owner, repo, path, ref=base_sha)
        if not payload:
            return ""
        files[path] = (str(payload.get("sha") or ""), "")
        return base64.b64decode(str(payload.get("content") or "")).decode("utf-8")

    changed = patches.apply(diff, reader=remote)
    branch = f"improvement/proposal-{proposal['id']}-{int(time.time())}"
    github.request("POST", f"/repos/{owner}/{repo}/git/refs", body={"ref": f"refs/heads/{branch}", "sha": base_sha})
    title = f"[SO] Improvement proposal {proposal['id']}: {str(proposal.get('title') or '')[:120]}"
    for path, (_, after) in changed.items():
        body: dict[str, Any] = {"message": title, "branch": branch,
                                "content": base64.b64encode(after.encode("utf-8")).decode("ascii")}
        if files.get(path, ("", ""))[0]:
            body["sha"] = files[path][0]
        github.request("PUT", f"/repos/{owner}/{repo}/contents/{path}", body=body)
    evaluation = proposal.get("evaluation") if isinstance(proposal.get("evaluation"), dict) else {}
    latest = ((proposal.get("canary") or {}).get("latest") or {}) if isinstance(proposal.get("canary"), dict) else {}
    pull = github.request("POST", f"/repos/{owner}/{repo}/pulls", body={
        "title": title, "head": branch, "base": base,
        "body": PR_BODY.format(
            actor=actor, title=proposal.get("title") or "", summary=proposal.get("summary") or "",
            type=proposal.get("type"), target=proposal.get("target") or "", metric=proposal.get("metric") or "",
            expected_effect=proposal.get("expected_effect") or "", risk=proposal.get("risk") or "",
            offline=f"{evaluation.get('status') or 'none'}: {evaluation.get('reason') or ''}",
            canary=str((latest.get("decision") or {}).get("reason") or "none"),
        ),
    })
    return {"method": "pull_request", "pr_url": str((pull or {}).get("html_url") or ""), "branch": branch,
            "repo": f"{owner}/{repo}", "base": base}


def promote(memory: Any, proposal: dict[str, Any], *, actor: str, tenant_id: str = "default",
            github: Any = None) -> dict[str, Any]:
    """A person approved the proposal: open a PR (or record a promotion by hand) and mark it promoted."""
    kind = proposal.get("type")
    if kind == "needs_design":
        raise ValueError("a needs-design proposal cannot be promoted: a person has to design the change")
    if kind == "knowledge_note":
        raise ValueError("approve the knowledge note itself in the Inbox")
    status = proposal.get("status")
    evaluation = proposal.get("evaluation") if isinstance(proposal.get("evaluation"), dict) else {}
    if status == "proposed" and evaluation.get("status") != pp.EVAL_UNAVAILABLE:
        raise ValueError("this proposal has an offline check; it is promoted after its canary")
    if status not in {"proposed", "awaiting_approval"}:
        raise ValueError(f"a {status} proposal cannot be promoted")
    promotion: dict[str, Any]
    if kind in {"skill_edit", "fragment_change"} and pr_target() is not None:
        try:
            promotion = open_pr(proposal, actor=actor, github=github)
        except Exception as exc:  # noqa: BLE001 - the person applies the diff instead
            logger.warning("promotion PR for proposal %s failed: %s", proposal.get("id"), exc)
            promotion = {"method": "manual", "reason": f"the pull request could not be opened: {str(exc)[:200]}"}
    else:
        promotion = {"method": "manual",
                     "reason": "no harness repository configured for pull requests (TS_HARNESS_IMPROVEMENT_PR_REPO)"
                     if kind != "setting_change" else "settings are applied by a person"}
    promotion.update(by=actor, at=_iso(_now()))
    moved = pp.move(memory, proposal, "promoted", actor=actor, reason=f"promoted ({promotion['method']})",
                    tenant_id=tenant_id, promotion=promotion)
    clear_cache()
    return moved


def reject(memory: Any, proposal: dict[str, Any], *, actor: str, reason: str = "", tenant_id: str = "default") -> dict[str, Any]:
    """A person rejected the proposal (a running canary is rolled back instead)."""
    status = proposal.get("status")
    if status == "canary":
        return rollback(memory, proposal, reason or f"stopped by {actor}", actor=actor, tenant_id=tenant_id)
    if status not in {"proposed", "awaiting_approval"}:
        raise ValueError(f"a {status} proposal cannot be rejected")
    moved = pp.move(memory, proposal, "rejected", actor=actor, reason=reason or f"rejected by {actor}", tenant_id=tenant_id)
    clear_cache()
    return moved
