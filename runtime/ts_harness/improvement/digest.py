"""The weekly failure-pattern digest: recurring problems across tickets -> concrete proposals.

Once a week (:func:`run_digest`, behind the ``improvement_digest`` system job) the tickets that
finished in the last 7 days are read and each one's failure signals collected (:func:`ticket_signals`):

* ``review:<category>``: blocking findings of the code review gate, by category;
* ``qa:<category>``: QA defects;
* ``repeated:<gate>``: the same finding came back in a later round of that gate;
* ``ci:<check>``: failed CI checks (the check name, without its matrix suffix);
* ``stuck:<reason>`` / ``arbiter:<category>``: why a step stopped for a person, and how the
  stuck-step arbiter classified it;
* ``readiness:questions``: the ticket was not ready and its questions went to a person;
* ``intervention:<kind>``: how a person stepped in (``guard.interventions``);
* ``post_merge:<signal>``: reverts, Jira bugs, nightly smoke failures after merge (merges of the
  last 21 days, so problems surface after the 14-day guard window opens).

A signal is a **pattern** when at least ``MIN_TICKETS`` (3) different tickets show it in the
window: one or two tickets are as likely a quirk of those tickets as a cause in the harness, and at
a few dozen tickets a week a higher bar would hide real recurring problems. Patterns are ranked by
ticket count. Patterns already covered by an open proposal, or by one rejected or rolled back in
the last ``COOLDOWN_DAYS`` (28), are skipped.

For each remaining pattern (at most ``WEEKLY_CAP`` (5) new proposals a week, counting earlier runs
of the same week) one model call through the improvement budget asks for ONE proposal
(``knowledge_note``, ``skill_edit``, ``setting_change`` or ``fragment_change``) with the metric it
should move, the expected effect and a risk note. :func:`proposals.screen` checks it: a target on
the off-limits list becomes ``needs_design`` (no diff, never acted on); a diff that does not apply
is dropped. Knowledge notes are created through the proposed-notes path (``source='drafted'``),
unless a near-duplicate note exists. When the budget runs out the digest stops quietly.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ts_harness.improvement import patches
from ts_harness.improvement import proposals as pp
from ts_harness.improvement.budget import BudgetExhausted, ImprovementBudget
from ts_harness.improvement.off_limits import OFF_LIMITS

logger = logging.getLogger(__name__)

DAYS = 7
GUARD_DAYS = 14
MIN_TICKETS = 3
WEEKLY_CAP = 5
COOLDOWN_DAYS = 28
MAX_EXAMPLES = 8
CALL_ESTIMATE_USD = 0.5
CADENCE_SECONDS = 7 * 86400 - 3600
STATE_NAMESPACE = "improvement-loop"
STATE_KEY = "digest"
REVIEW_GATE = "finalize_code_review"
QA_GATE = "qa_gate"
CI_STEPS = frozenset({"await_pr_checks", "await_ci_on_sha"})
CONTEXT_FILES = {
    "review": pp.GATE_SKILL_PATH,
    "repeated": pp.GATE_SKILL_PATH,
    "qa": "skills/qa/governed-qa-gate/SKILL.md",
    "ci": "skills/coding-backend/implement-work-order/SKILL.md",
}
MAX_CONTEXT_CHARS = 18000
KIND_ORDER = ("post_merge", "intervention", "repeated", "review", "qa", "ci", "stuck", "arbiter", "readiness")

ModelCall = Callable[[str], str]


def enabled() -> bool:
    """``TS_HARNESS_IMPROVEMENT_DIGEST`` (default on)."""
    return (os.environ.get("TS_HARNESS_IMPROVEMENT_DIGEST") or "on").strip().lower() not in {"0", "off", "false", "no"}


def _input(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("input") if isinstance(task.get("input"), dict) else {}


def _output(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("output") if isinstance(task.get("output"), dict) else {}


def _slug(text: Any) -> str:
    value = re.sub(r"\(.*?\)|\[.*?\]", "", str(text or "").lower())
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:60] or "unknown"


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# Signals


def ticket_signals(tasks: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Failure signals of one ticket: ``{key: [examples]}`` (examples are short texts)."""
    from ts_harness.improvement.guard import interventions
    from ts_harness.workflows import revision_guidance as rg
    from ts_harness.workflows.code_review import is_blocking

    found: dict[str, list[str]] = {}

    def add(key: str, example: str = "") -> None:
        bucket = found.setdefault(key, [])
        text = " ".join(str(example or "").split())[:240]
        if text and text not in bucket and len(bucket) < 3:
            bucket.append(text)

    ordered = sorted((t for t in tasks if isinstance(t, dict)), key=lambda t: str(t.get("created_at") or ""))
    for gate, prefix in ((REVIEW_GATE, "review"), (QA_GATE, "qa")):
        rounds = rg.review_rounds(ordered, gate)
        for review in rounds:
            for item in review["findings"]:
                if prefix == "review" and not is_blocking(item):
                    continue
                category = item.get("category") or ("defect" if prefix == "qa" else "other")
                add(f"{prefix}:{_slug(category)}", item.get("message") or item.get("description") or "")
        repeated: list[dict[str, Any]] = []
        for end in range(2, len(rounds) + 1):
            repeated.extend(rg.repeated_findings(rounds[:end]))
        for item in repeated:
            add(f"repeated:{gate}", item.get("message") or "")
    for task in ordered:
        data, output = _input(task), _output(task)
        step = str(task.get("step_name") or "")
        if step in CI_STEPS:
            for name in output.get("failed_checks") or []:
                add(f"ci:{_slug(name)}", str(name))
        waiting = data.get("_awaiting_human_decision") if isinstance(data.get("_awaiting_human_decision"), dict) else {}
        reason = str(waiting.get("reason") or "")
        if reason == "ticket_not_ready" or (isinstance(output.get("readiness"), dict)
                                            and output["readiness"].get("questions") and task.get("status") == "blocked"):
            questions = (output.get("readiness") or {}).get("questions") if isinstance(output.get("readiness"), dict) else []
            add("readiness:questions", "; ".join(str(q) for q in (questions or [])[:3]) or str(task.get("error") or ""))
        elif reason:
            add(f"stuck:{_slug(reason)}", str(task.get("error") or "")[:240])
        arbiter = data.get("_arbiter") if isinstance(data.get("_arbiter"), dict) else {}
        if arbiter.get("category"):
            add(f"arbiter:{_slug(arbiter['category'])}", str(arbiter.get("reason") or arbiter.get("why") or ""))
    for kind in interventions(ordered):
        add(f"intervention:{kind}")
    return found


def _pr_url(tasks: list[dict[str, Any]]) -> str:
    for task in sorted(tasks, key=lambda t: str(t.get("created_at") or ""), reverse=True):
        if _output(task).get("pr_url"):
            return str(_output(task)["pr_url"])
    return ""


def _jira_key(tasks: list[dict[str, Any]]) -> str:
    return next((str(_input(t).get("jira_issue_key")) for t in tasks if _input(t).get("jira_issue_key")), "")


def collect(
    memory: Any,
    *,
    tenant_id: str = "default",
    now: datetime | None = None,
    days: int = DAYS,
    runs: Iterable[dict[str, Any]] | None = None,
    guard_context: Any = None,
) -> list[dict[str, Any]]:
    """One record per ticket: ``{workflow_id, program_id, project, jira_issue_key, pr_url, signals}``.

    Failure signals come from tickets finished in the last ``days``; post-merge signals from merges
    in the last ``days + GUARD_DAYS``."""
    from ts_harness.improvement import guard as gd
    from ts_harness.workflows import live_view as lv

    now = now or datetime.now(timezone.utc)
    recent_cut = now - timedelta(days=days)
    if runs is None:
        runs = lv.period_runs(memory, tenant_id=tenant_id, days=days + GUARD_DAYS, now=now)
    ctx = guard_context or gd.GuardContext(days=GUARD_DAYS, now=now, memory=memory)
    tickets = []
    for run in runs:
        tasks = [t for t in run.get("tasks") or [] if isinstance(t, dict)]
        workflow = run.get("workflow") or {}
        ended = run.get("ended") or {}
        finished = _parse(ended.get("finished_at"))
        pr_url = _pr_url(tasks)
        record = {
            "workflow_id": str(workflow.get("id") or ""),
            "program_id": str(run.get("program_id") or ""),
            "project": str(run.get("project") or ""),
            "jira_issue_key": _jira_key(tasks),
            "pr_url": pr_url,
            "outcome": str(ended.get("outcome") or ""),
            "signals": {},
        }
        if finished is None or finished >= recent_cut:
            record["signals"] = ticket_signals(tasks)
        if record["outcome"] == "merged" and finished is not None:
            row = {"outcome": "merged", "pr_url": pr_url, "jira_issue_key": record["jira_issue_key"], "repo": lv._repo(pr_url)}
            try:
                guard = gd.post_merge(ctx, row, tasks, finished)
            except Exception as exc:  # noqa: BLE001 - unknown, not an error
                logger.info("post-merge signals unavailable for %s: %s", record["workflow_id"], exc)
                guard = {}
            for name in ("reverts", "jira_bugs", "smoke_failures"):
                if int(guard.get(name) or 0) > 0:
                    record["signals"].setdefault(f"post_merge:{name}", [f"{guard[name]} {name.replace('_', ' ')}"])
        if record["signals"]:
            tickets.append(record)
    return tickets


def patterns(tickets: list[dict[str, Any]], *, min_tickets: int = MIN_TICKETS) -> list[dict[str, Any]]:
    """Signals shown by at least ``min_tickets`` tickets, most tickets first."""
    by_key: dict[str, dict[str, Any]] = {}
    for ticket in tickets:
        for key, examples in ticket["signals"].items():
            entry = by_key.setdefault(key, {"key": key, "kind": key.split(":", 1)[0], "tickets": [], "examples": []})
            entry["tickets"].append({k: ticket[k] for k in ("workflow_id", "program_id", "project", "jira_issue_key", "pr_url")})
            for example in examples:
                if example and len(entry["examples"]) < MAX_EXAMPLES and example not in entry["examples"]:
                    entry["examples"].append(example)
    found = [entry for entry in by_key.values() if len(entry["tickets"]) >= min_tickets]
    rank = {kind: index for index, kind in enumerate(KIND_ORDER)}
    found.sort(key=lambda e: (-len(e["tickets"]), rank.get(e["kind"], 99), e["key"]))
    for entry in found:
        entry["count"] = len(entry["tickets"])
    return found


# Proposals


PROMPT = """You improve an automated software delivery harness. Coding agents implement tickets; a
pre-push reviewer, a code review gate and a QA gate check the work; people step in when it stalls.
The goal is more tickets merged without a person stepping in, without more problems after merge.

This failure pattern recurred across {count} tickets in the last week:
  pattern: {key}
  meaning: {meaning}
Examples (from different tickets):
{examples}

Propose ONE concrete change that would make this pattern rarer. Choose the type that fits best:
- "knowledge_note": a short lesson every future coding run reads (best for project-specific lessons);
- "skill_edit": a unified diff to a skill file under skills/ (the instructions an agent or reviewer follows);
- "setting_change": a program default or runtime setting, with its old and new value;
- "fragment_change": a unified diff to a workflow definition under config/fragments/ or config/workflows/.
{context}
Never propose changes to any of these (they need a person): security rules, what blocks a PR (gate
severities, verdicts, output contracts), permissions, budgets, the stuck-step arbiter's limits,
the metrics or guard code:
{off_limits}

A diff must apply exactly to the file text shown: copy context lines verbatim, use "--- a/<path>"
and "+++ b/<path>" headers and correct "@@ -l,n +l,n @@" hunk headers, and change as little as possible.
Do not repeat an open proposal:
{open}

Reply with JSON only, either {{"skip": true, "reason": "..."}} when no harness change would help, or:
{{"type": "knowledge_note" | "skill_edit" | "setting_change" | "fragment_change",
  "title": "at most 100 characters",
  "summary": "what changes and why, at most 600 characters",
  "metric": "autonomy" | "first_try" | "cost" | "time_to_merge" | "problems_after_merge",
  "expected_effect": "the change you expect in that metric and why",
  "risk": "what could get worse",
  "note": {{"scope": "project" or "org", "kind": "avoid" or "howto", "title": "...", "body": "at most 280 characters", "why": "...", "applies_to": ["globs or stage names"]}},
  "diff": "the unified diff (skill_edit, fragment_change)",
  "setting": {{"name": "dotted.setting.name or ENV_VAR", "old": "current value", "new": "proposed value"}}}}
"""

MEANINGS = {
    "review": "the code review gate blocked PRs with findings of this category",
    "qa": "the QA gate failed PRs with defects of this kind",
    "repeated": "the same finding came back after a revision, in this gate",
    "ci": "this CI check failed on the PR",
    "stuck": "a step stopped and waited for a person for this reason",
    "arbiter": "the stuck-step arbiter classified a stopped step this way",
    "readiness": "a hand-written ticket was not ready to build and its questions went to a person",
    "intervention": "a person had to step in this way before the merge",
    "post_merge": "a problem showed up after the PR merged",
}


def _context(kind: str, *, reader: Callable[[str], str]) -> str:
    path = CONTEXT_FILES.get(kind)
    if not path:
        return ""
    text = reader(path)
    if not text:
        return ""
    lines = [f"\nThe file most likely to need the change, {path}:", "```", text[:MAX_CONTEXT_CHARS], "```"]
    if path == pp.GATE_SKILL_PATH:
        lines.append("The pre-push reviewer reads these sections of it; an edit to them can be checked offline "
                     f"and tried on live tickets: {', '.join(pp.editable_pre_push_sections())}.")
    return "\n".join(lines) + "\n"


def build_prompt(pattern: dict[str, Any], *, open_titles: list[str], reader: Callable[[str], str]) -> str:
    off = "\n".join(f"- {entry['match']} ({entry['why']})" for entry in OFF_LIMITS)
    return PROMPT.format(
        count=pattern["count"],
        key=pattern["key"],
        meaning=MEANINGS.get(pattern["kind"], pattern["kind"]),
        examples="\n".join(f"- {text}" for text in pattern["examples"]) or "- (no text recorded)",
        context=_context(pattern["kind"], reader=reader),
        off_limits=off,
        open="\n".join(f"- {title}" for title in open_titles[:20]) or "- (none)",
    )


def parse_reply(reply: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", str(reply or ""), re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def evidence(pattern: dict[str, Any], digest_id: str) -> dict[str, Any]:
    return {
        "digest_id": digest_id,
        "pattern": pattern["key"],
        "kind": pattern["kind"],
        "tickets": pattern["count"],
        "refs": pattern["tickets"][:20],
        "examples": pattern["examples"],
    }


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _note_evidence(pattern: dict[str, Any]) -> list[dict[str, str]]:
    items = [{"type": "drafted_from", "ref": f"digest:{pattern['key']}"[:200], "url": "",
              "excerpt": f"{pattern['count']} tickets: {MEANINGS.get(pattern['kind'], pattern['kind'])}"[:300]}]
    for ref in pattern["tickets"][:10]:
        if ref.get("workflow_id"):
            items.append({"type": "workflow", "ref": ref["workflow_id"][:200], "url": "", "excerpt": ref.get("jira_issue_key") or ""})
        if ref.get("pr_url"):
            items.append({"type": "pr", "ref": "", "url": ref["pr_url"][:500], "excerpt": ""})
    return items[:20]


def create_note(memory: Any, note: dict[str, Any], pattern: dict[str, Any], *, tenant_id: str) -> dict[str, Any] | None:
    """The proposed note through the drafted-notes path, or None when it duplicates an existing note."""
    from ts_harness.learning import failure_drafts as fd

    projects = {ref.get("project") for ref in pattern["tickets"] if ref.get("project")}
    project_id = next(iter(projects)) if len(projects) == 1 else ""
    scope = note["scope"] if note["scope"] == "org" or project_id else "org"
    existing = fd._existing(memory, tenant_id, project_id)
    if fd.near_duplicate(note, existing, similar=fd._similar(memory, tenant_id, note)) is not None:
        return None
    body: dict[str, Any] = {
        "tenant_id": tenant_id, "scope": scope, "kind": note["kind"], "title": note["title"], "body": note["body"],
        "why": note["why"], "applies_to": note["applies_to"], "evidence": _note_evidence(pattern), "source": "drafted",
        "status": "proposed", "proposal_action": "add", "actor": pp.ACTOR,
        "change_reason": f"proposed by the weekly failure digest ({pattern['key']})",
    }
    if scope == "project":
        body["project_id"] = project_id
    created = memory.create_knowledge_note(**body)
    return created if isinstance(created, dict) else {"id": created}


def _state(memory: Any) -> tuple[dict[str, Any], int]:
    current = memory.kv_get(STATE_NAMESPACE, STATE_KEY) or {}
    value = current.get("value") if isinstance(current.get("value"), dict) else {}
    return dict(value), int(current.get("version") or 0)


def run_digest(
    *,
    memory: Any = None,
    tenant_id: str = "default",
    now: datetime | None = None,
    model_call: ModelCall | None = None,
    budget: ImprovementBudget | None = None,
    reader: Callable[[str], str] | None = None,
    runs: Iterable[dict[str, Any]] | None = None,
    guard_context: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """System job: once a week, find recurring failure patterns and propose one change for each."""
    from ts_harness.improvement import llm

    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    if not enabled():
        return {"status": "skipped", "reason": "TS_HARNESS_IMPROVEMENT_DIGEST is off"}
    now = now or datetime.now(timezone.utc)
    state, version = _state(memory)
    last = _parse(state.get("last_run_at"))
    if not force and last is not None and (now - last).total_seconds() < CADENCE_SECONDS:
        return {"status": "skipped", "reason": "ran this week", "last_run_at": state.get("last_run_at")}
    budget = budget or ImprovementBudget(tenant_id=tenant_id, memory=memory)
    digest_id = f"digest-{now.strftime('%Y-%m-%d')}"
    if model_call is None:
        model_call = llm.metered_call("weekly_digest", ref=digest_id, budget=budget, estimate_usd=CALL_ESTIMATE_USD,
                                      tenant_id=tenant_id)
        if model_call is None:
            return {"status": "skipped", "reason": "no improvement model configured"}
    if not budget.can_spend(CALL_ESTIMATE_USD):
        return {"status": "paused", "reason": "the improvement budget is used up"}
    read = reader or (lambda path: patches.read_file(path))

    tickets = collect(memory, tenant_id=tenant_id, now=now, runs=runs, guard_context=guard_context)
    found = patterns(tickets)
    every = memory.list_improvement_proposals(tenant_id=tenant_id, limit=200)
    open_rows = [p for p in every if p.get("status") in pp.OPEN]
    cool_cut = now - timedelta(days=COOLDOWN_DAYS)
    week_cut = now - timedelta(days=DAYS)
    blocked_keys = {p.get("pattern_key") for p in open_rows} | {
        p.get("pattern_key") for p in every
        if p.get("status") in {"rejected", "rolled_back"} and (_parse(p.get("updated_at")) or now) >= cool_cut
    }
    made_this_week = sum(1 for p in every if p.get("created_by") == pp.ACTOR and (_parse(p.get("created_at")) or now) >= week_cut)
    room = max(0, WEEKLY_CAP - made_this_week)
    open_titles = [str(p.get("title") or "") for p in open_rows]
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    calls = 0
    paused = ""
    spent_before = float(getattr(model_call, "spent", 0.0) or 0.0)
    for pattern in found:
        if len(created) >= room:
            skipped.append({"pattern": pattern["key"], "reason": f"weekly cap of {WEEKLY_CAP} reached"})
            continue
        if pattern["key"] in blocked_keys:
            skipped.append({"pattern": pattern["key"], "reason": "covered by an open or recently declined proposal"})
            continue
        if not budget.can_spend(CALL_ESTIMATE_USD):
            paused = "the improvement budget is used up"
            break
        before = float(getattr(model_call, "spent", 0.0) or 0.0)
        try:
            reply = model_call(build_prompt(pattern, open_titles=open_titles, reader=read))
        except BudgetExhausted:
            paused = "the improvement budget is used up"
            break
        except Exception as exc:  # noqa: BLE001 - the next pattern may still work
            skipped.append({"pattern": pattern["key"], "reason": f"model call failed: {str(exc)[:160]}"})
            continue
        calls += 1
        cost = round(float(getattr(model_call, "spent", 0.0) or 0.0) - before, 4)
        draft = parse_reply(reply)
        if not draft or draft.get("skip"):
            skipped.append({"pattern": pattern["key"], "reason": str((draft or {}).get("reason") or "no proposal")[:200]})
            continue
        try:
            screened = pp.screen(draft, reader=read)
        except pp.Rejected as exc:
            skipped.append({"pattern": pattern["key"], "reason": f"dropped: {exc}"[:240]})
            continue
        title = str(draft.get("title") or "").strip()[:200] or f"Proposal for {pattern['key']}"
        if any(_norm(title) == _norm(t) for t in open_titles):
            skipped.append({"pattern": pattern["key"], "reason": "same title as an open proposal"})
            continue
        change = dict(screened["change"])
        evaluation: dict[str, Any] | None = None
        if screened["type"] == "knowledge_note":
            try:
                note = create_note(memory, change["note"], pattern, tenant_id=tenant_id)
            except Exception as exc:  # noqa: BLE001
                skipped.append({"pattern": pattern["key"], "reason": f"note not created: {str(exc)[:160]}"})
                continue
            if note is None:
                skipped.append({"pattern": pattern["key"], "reason": "near-duplicate of an existing note"})
                continue
            change["note_id"] = str(note.get("id") or "")
            evaluation = {"status": pp.EVAL_NOT_NEEDED, "reason": "a person reviews the note in the Inbox"}
        elif screened["type"] == "needs_design":
            evaluation = {"status": pp.EVAL_NOT_NEEDED, "reason": "off limits: a person has to design this change"}
        elif not screened["offline_check"]:
            evaluation = {"status": pp.EVAL_UNAVAILABLE, "reason": pp.NO_OFFLINE_CHECK}
        else:
            change["offline_check"] = screened["offline_check"]
        try:
            row = memory.create_improvement_proposal(
                tenant_id=tenant_id, type=screened["type"], target=screened["target"], title=title,
                summary=str(draft.get("summary") or "")[:4000], pattern_key=pattern["key"],
                metric=str(draft.get("metric") or "autonomy")[:300],
                expected_effect=str(draft.get("expected_effect") or "")[:1000], risk=str(draft.get("risk") or "")[:1000],
                change=change, evidence=evidence(pattern, digest_id), evaluation=evaluation, cost_usd=max(0.0, cost),
                digest_id=digest_id, actor=pp.ACTOR, reason=f"weekly digest {digest_id}",
            )
        except Exception as exc:  # noqa: BLE001
            skipped.append({"pattern": pattern["key"], "reason": f"not stored: {str(exc)[:160]}"})
            continue
        created.append(row)
        open_titles.append(title)
        blocked_keys.add(pattern["key"])
    spent = round(float(getattr(model_call, "spent", 0.0) or 0.0) - spent_before, 4)
    if not paused:
        new_state = {"last_run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "digest_id": digest_id,
                     "patterns": len(found), "created": [r.get("id") for r in created]}
        try:
            memory.kv_put(STATE_NAMESPACE, STATE_KEY, new_state, expected_version=version)
        except Exception as exc:  # noqa: BLE001 - another worker recorded the run
            logger.info("digest state not saved: %s", exc)
    return {
        "status": "paused" if paused else "ok",
        "reason": paused,
        "digest_id": digest_id,
        "tickets": len(tickets),
        "patterns": [{"key": p["key"], "tickets": p["count"]} for p in found],
        "created": [{"id": r.get("id"), "type": r.get("type"), "title": r.get("title")} for r in created],
        "skipped": skipped,
        "model_calls": calls,
        "cost_usd": spent,
    }
