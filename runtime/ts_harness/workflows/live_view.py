"""What is happening now, and where the time went.

* :func:`live` lists every running ticket or work order across running programs: its current
  step, how long it has been there, who it is waiting on (the agent, CI, or you), the coding
  job's latest output, and the coding capacity in use.
* :func:`program_board` shows one program's queue: each ticket's column (waiting on blockers,
  ready, coding, in review, waiting for you, done), its blockers, a per-step timeline, and
  totals of where the time went.

Durations come from the task timestamps: a step starts when it was claimed (``started_at``),
or, while still pending, when the steps it depends on finished; it ends at ``completed_at``,
at its last update when it stopped, or now while it is still going.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ts_harness.workflows import phase_activity as pa
from ts_harness.workflows.work_order_waves import awaiting_human_decision

TICKET_TYPES = {"implement_jira_ticket", "deliver_work_order", "implement_ticket", "bug_work_order"}
CATEGORIES = {
    "await_pr_checks": "ci",
    "review_precheck": "review",
    "code_review": "review",
    "finalize_code_review": "review",
    "qa_gate": "qa",
    "ensure_fresh": "ci",
    "await_human_merge": "merge",
    "close_ticket": "other",
    "prepare_ticket_delivery": "other",
}
CATEGORY_LABELS = {"coding": "Coding", "ci": "CI checks", "review": "Code review", "qa": "QA",
                   "merge": "Waiting for merge", "decision": "Waiting for a decision", "paused": "Paused",
                   "other": "Other"}
COLUMNS = ("waiting_on_blockers", "ready", "coding", "in_review", "waiting_for_you", "paused", "done", "stopped")


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _category(task: dict[str, Any], coding_step: str) -> str:
    step = str(task.get("step_name") or "")
    if str(task.get("status") or "") == "blocked" and str(task.get("error") or "") == pa.PAUSE_WAIT:
        return "paused"  # held by a pause: not coding time, not stuck time
    if step == coding_step or task.get("agent_id") == pa.CODING_AGENT:
        return "coding"
    if awaiting_human_decision(task):
        return "decision"
    return CATEGORIES.get(step, "other")


def step_timeline(tasks: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every run of every step, oldest first, with its start, end and duration in seconds."""
    now = now or datetime.now(timezone.utc)
    by_id = {str(t.get("id") or ""): t for t in tasks}
    ordered = sorted(tasks, key=lambda t: str(t.get("created_at") or ""))
    coding_step = next((str(t.get("step_name") or "") for t in ordered if t.get("agent_id") == pa.CODING_AGENT), "")
    runs: dict[str, int] = {}
    bars = []
    for task in ordered:
        status = str(task.get("status") or "")
        if status == "cancelled" and not task.get("started_at"):
            continue  # superseded before it ever ran
        if status == "pending":
            continue  # not started yet: shown as the queue, not as a bar
        deps = [by_id.get(str(dep)) or {} for dep in task.get("depends_on") or []]
        ready = max((d for d in (_parse(dep.get("completed_at")) for dep in deps) if d), default=None)
        # A step starts when it became ready: a parked step (coding job, CI, merge wait) is
        # claimed again when it resumes, so its started_at is the resume, not the start.
        claimed = _parse(task.get("started_at"))
        created = _parse(task.get("created_at"))
        # A revision depends on a step that finished long before it was created (the earlier
        # attempt); it cannot have started before it existed, so the wait for a decision is not
        # counted as its run.
        if ready and created and created > ready:
            ready = created
        start = ready or claimed or created
        if claimed and ready and claimed < ready:
            start = claimed
        end = _parse(task.get("completed_at"))
        if end is None:
            end = now if status in {"running", "claimed", "blocked"} and not str(task.get("error") or "").startswith(
                ("stopped_by_human:",)) else _parse(task.get("updated_at")) or now
        step = str(task.get("step_name") or "")
        runs[step] = runs.get(step, 0) + 1
        bars.append({
            "task_id": str(task.get("id") or ""),
            "step_name": step,
            "label": pa._label(step, coding_step),
            "category": _category(task, coding_step),
            "status": "coding" if pa._coding(task) else status,
            "run": runs[step],
            "start": _iso(start),
            "end": _iso(end),
            "seconds": max(0, int((end - start).total_seconds())) if start and end else 0,
            "open": task.get("completed_at") is None and status not in {"succeeded", "cancelled", "failed"},
        })
    return bars


def waiting_on(summary: dict[str, Any]) -> str:
    """``you``, ``ci``, ``agent``, ``queue`` or ``done``: who the item is waiting on right now."""
    kind = summary.get("kind")
    if kind in {"done"}:
        return "done"
    if kind in {"stuck", "failed", "blocked"}:
        return "you"
    if kind == "paused":
        return "paused"
    current = str(summary.get("current_step") or "")
    state = str(summary.get("state") or "")
    if current == "await_human_merge" or state.startswith("Waiting for approval") or state == "Ready to merge":
        return "you"
    if current in {"await_pr_checks", "ensure_fresh"}:
        return "ci"
    if state == "OpenCode queued":
        return "queue"
    return "agent"


def _current_since(summary: dict[str, Any], bars: list[dict[str, Any]]) -> str:
    open_bars = [bar for bar in bars if bar["open"] and bar["step_name"] == summary.get("current_step")]
    return open_bars[-1]["start"] if open_bars else ""


def item_view(memory: Any, workflow: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """One ticket or work order: the phase-activity summary plus timing and who it waits on."""
    now = now or datetime.now(timezone.utc)
    summary = pa._with_coding_job(memory, pa.summarize_workflow(workflow))
    tasks = [t for t in workflow.get("tasks") or [] if isinstance(t, dict)]
    bars = step_timeline(tasks, now=now)
    since = _current_since(summary, bars)
    started = min((_parse(bar["start"]) for bar in bars if bar["start"]), default=None)
    # A finished run's total ends where its last step ended, not now.
    running = str(workflow.get("status") or "") == "running"
    last_end = max((d for d in (_parse(bar["end"]) for bar in bars) if d), default=None)
    end_of_run = now if running or last_end is None else last_end
    key = next((str((t.get("input") or {}).get("jira_issue_key") or "") for t in tasks
                if isinstance(t.get("input"), dict) and (t.get("input") or {}).get("jira_issue_key")), "")
    automatic = sum(1 for t in tasks if isinstance(t.get("input"), dict) and (t["input"].get("_auto_grant") or t["input"].get("_guided")))
    return {
        **{k: summary[k] for k in ("workflow_id", "workflow_type", "label", "state", "kind", "current_step",
                                   "revisions", "pr_url", "stuck_task_id", "coding_job")},
        "jira_issue_key": key,
        "waiting_on": waiting_on(summary),
        "in_step_since": since,
        "in_step_seconds": int((now - _parse(since)).total_seconds()) if _parse(since) else 0,
        "started_at": _iso(started),
        "elapsed_seconds": max(0, int((end_of_run - started).total_seconds())) if started else 0,
        "stronger_model": automatic > 0,
        "timeline": bars,
    }


def _workflow(memory: Any, workflow_id: str) -> dict[str, Any]:
    return pa._workflow_record(memory, workflow_id)


def _descendants(memory: Any, workflow_id: str, depth: int = 0, chain: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Workflows below ``workflow_id``; each carries ``_chain``, its id then its parents' (for pauses)."""
    if depth > 3:
        return []
    chain = chain or (workflow_id,)
    found = []
    for child in memory.get_workflow_children(workflow_id) or []:
        child_id = str(child.get("id") or "")
        if not child_id:
            continue
        found.append({**child, "_chain": [child_id, *chain]})
        found.extend(_descendants(memory, child_id, depth + 1, (child_id, *chain)))
    return found


def _program_items(memory: Any, program_id: str, *, running_only: bool) -> list[dict[str, Any]]:
    """The program's ticket and work-order workflows (its own and those its phases started)."""
    workflows = (memory.get_program(program_id) or {}).get("workflows") or []
    seen: dict[str, dict[str, Any]] = {}
    for record in workflows:
        wid = str(record.get("id") or "")
        candidates = [{**record, "_chain": [wid]}, *_descendants(memory, wid)]
        for candidate in candidates:
            cid = str(candidate.get("id") or "")
            if not cid or str(candidate.get("workflow_type") or "") not in TICKET_TYPES:
                continue
            if running_only and str(candidate.get("status") or "") != "running":
                continue
            seen[cid] = candidate
    return list(seen.values())


def program_chain(program: dict[str, Any]) -> list[str]:
    ids = [str(program.get(key) or "") for key in ("id", "parent_program_id", "root_program_id")]
    return [pid for i, pid in enumerate(ids) if pid and pid not in ids[:i]]


def with_pause(view: dict[str, Any], state: dict[str, Any], *, workflow_chain: list[str], programs: list[str]) -> dict[str, Any]:
    """``paused``: the pause covering the item (scope, who, when, reason), or None. ``held`` is the
    kind of run waiting for the resume when one is (``revision``, ``first_run``...)."""
    from ts_harness.workflows import pause

    entry = pause.entry_for(state, workflow_chain=workflow_chain, program_chain=programs)
    view["paused"] = {k: entry.get(k) for k in ("scope", "target_id", "by", "at", "reason", "label")} if entry else None
    view.setdefault("held", "")
    return view


def _held_kind(workflow: dict[str, Any]) -> str:
    from ts_harness.workflows.pause import HOLD_KEY

    for task in workflow.get("tasks") or []:
        if isinstance(task, dict) and task.get("status") == "blocked" and task.get("error") == pa.PAUSE_WAIT:
            hold = (task.get("input") or {}).get(HOLD_KEY) if isinstance(task.get("input"), dict) else None
            return str((hold or {}).get("kind") or "held") if isinstance(hold, dict) else "held"
    return ""


def coding_capacity(memory: Any) -> dict[str, Any]:
    import os

    try:
        jobs = memory.list_coding_jobs(status="queued,starting,running", limit=100)
    except Exception:  # noqa: BLE001 - capacity is optional
        jobs = []
    running = sum(1 for job in jobs if job.get("status") in {"starting", "running"})
    queued = sum(1 for job in jobs if job.get("status") == "queued")
    limit = os.environ.get("CODING_MAX_RUNNING")
    return {"running": running, "queued": queued, "limit": int(limit) if str(limit or "").isdigit() else None}


def live(memory: Any, *, tenant_id: str = "default", allowed: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """Every running ticket or work order across running programs, waiting-for-you first."""
    from ts_harness.workflows import pause

    now = now or datetime.now(timezone.utc)
    state = pause.read_state(memory)
    programs = (memory.list_programs(tenant_id=tenant_id, status="running", limit=100) or {}).get("programs") or []
    items = []
    for program in programs:
        program_id = str(program.get("id") or "")
        if not program_id or (allowed is not None and not allowed(program_id)):
            continue
        for record in _program_items(memory, program_id, running_only=True):
            workflow = _workflow(memory, str(record["id"]))
            view = item_view(memory, workflow, now=now)
            view.update({"program_id": program_id,
                         "program_label": str(program.get("display_name") or program.get("summary") or program_id)[:120],
                         "held": _held_kind(workflow)})
            with_pause(view, state, workflow_chain=list(record.get("_chain") or [record["id"]]), programs=program_chain(program))
            items.append(view)
    order = {"you": 0, "paused": 1, "agent": 2, "ci": 3, "queue": 4, "done": 5}
    items.sort(key=lambda item: (order.get(item["waiting_on"], 6), -item["in_step_seconds"]))
    return {"generated_at": _iso(now), "items": items, "capacity": coding_capacity(memory),
            "paused": pause.global_entry(state)}


def _column(view: dict[str, Any] | None, *, done: bool, blocked_by: list[str]) -> str:
    if done:
        return "done"
    if view is None:
        return "waiting_on_blockers" if blocked_by else "ready"
    if view["kind"] == "done":
        return "done"
    if view["kind"] == "failed":
        return "stopped"
    if view["kind"] == "paused":
        return "paused"  # held: its next coding run (or new tickets) waits for the resume
    if view["waiting_on"] == "you":
        return "waiting_for_you"
    if view["current_step"] in pa.STEP_LABELS:
        return "in_review"
    return "coding"


def program_board(memory: Any, program_id: str, *, jira: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """The program's queue as a board, each item's timeline, and totals of where the time went."""
    now = now or datetime.now(timezone.utc)
    program = (memory.get_program(program_id) or {}).get("program") or {}
    meta = program.get("metadata")
    if isinstance(meta, str):
        import json

        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    meta = meta if isinstance(meta, dict) else {}
    artifacts = meta.get("artifacts") if isinstance(meta.get("artifacts"), dict) else {}
    keys = [str(k) for k in artifacts.get("jira_issue_keys") or []]
    completed = {str(k) for k in artifacts.get("completed_jira_issue_keys") or []}
    views: dict[str, dict[str, Any]] = {}
    attempts: dict[str, list[dict[str, Any]]] = {}
    from ts_harness.workflows import pause

    state = pause.read_state(memory)
    programs = program_chain({**program, "id": program.get("id") or program_id})
    for record in sorted(_program_items(memory, program_id, running_only=False), key=lambda r: str(r.get("created_at") or "")):
        workflow = _workflow(memory, str(record["id"]))
        view = item_view(memory, workflow, now=now)
        if str(workflow.get("status") or "") != "running":
            view.update(outcome(workflow, [t for t in workflow.get("tasks") or [] if isinstance(t, dict)]))
            view.update({"paused": None, "held": ""})
        else:
            view["held"] = _held_kind(workflow)
            with_pause(view, state, workflow_chain=list(record.get("_chain") or [record["id"]]), programs=programs)
        label = view["jira_issue_key"] or view["label"]
        if label not in keys:
            keys.append(label)
        attempts.setdefault(label, []).append(view)
        views[label] = view  # the latest attempt is the item; earlier ones are its attempts
    blockers: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    jira_done: set[str] = set()
    if jira is None and any("-" in key for key in keys):
        try:
            from ts_harness.integrations.jira_client import JiraClient

            jira = JiraClient.from_env()
        except Exception:  # noqa: BLE001 - the board works without Jira
            jira = None
    if jira is not None:
        from ts_harness.jira.spec_hydrate import open_blockers

        for key in keys:
            try:
                issue = jira.get_issue(key) or {}
            except Exception:  # noqa: BLE001
                continue
            fields = issue.get("fields") or {}
            titles[key] = str(fields.get("summary") or "")[:120]
            blockers[key] = open_blockers(fields.get("issuelinks"))
            category = str(((fields.get("status") or {}).get("statusCategory") or {}).get("key") or "")
            if category == "done":
                jira_done.add(key)
    items, totals = [], {}
    for key in keys:
        view = views.get(key)
        column = _column(view, done=key in completed or key in jira_done, blocked_by=blockers.get(key, []))
        for run in attempts.get(key, []) or ([view] if view else []):
            for bar in run.get("timeline") or []:
                totals[bar["category"]] = totals.get(bar["category"], 0) + bar["seconds"]
        earlier = list(reversed(attempts.get(key, [])[:-1]))
        items.append({"key": key, "title": titles.get(key, (view or {}).get("label", "")), "column": column,
                      "blocked_by": blockers.get(key, []), "item": view, "attempts": earlier})
    total = sum(totals.values()) or 1
    return {
        "generated_at": _iso(now),
        "program_id": program_id,
        "columns": list(COLUMNS),
        "items": items,
        "totals": [{"category": c, "label": CATEGORY_LABELS.get(c, c), "seconds": s, "share": round(s / total, 3)}
                   for c, s in sorted(totals.items(), key=lambda kv: -kv[1])],
        "parallel_limit": artifacts.get("max_parallel_tickets"),
        "paused": pause.entry_for(state, program_chain=programs, include_global=True),
    }


# --- history -------------------------------------------------------------------------------------

OUTCOMES = ("merged", "accepted", "stopped", "cancelled", "failed", "replaced", "done")


def _who(actor: str) -> str:
    actor = str(actor or "").strip()
    if actor in {"harness-triage", "workflow-runner", "orchestrator"}:
        return "harness"
    return actor


def outcome(workflow: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, str]:
    """How a finished run ended (``OUTCOMES``), who decided, and when it finished."""
    from ts_harness.workflows.human_decisions import ACCEPT_KEY, STOPPED_PREFIX

    status = str(workflow.get("status") or "")
    finished = _parse(workflow.get("completed_at")) or max(
        (d for d in (_parse(t.get("completed_at") or t.get("updated_at")) for t in tasks) if d), default=None)
    result = {"outcome": "done", "decided_by": "", "finished_at": _iso(finished)}
    stopped = next((t for t in tasks if str(t.get("error") or "").startswith(STOPPED_PREFIX)), None)
    if stopped is not None:
        actor = str(stopped["error"])[len(STOPPED_PREFIX):].split(":", 1)[0].strip()
        return {**result, "outcome": "stopped", "decided_by": _who(actor)}
    merge = next((t for t in sorted(tasks, key=lambda t: str(t.get("created_at") or ""), reverse=True)
                  if t.get("step_name") == "await_human_merge" and t.get("status") == "succeeded"), None)
    if merge is not None:
        output = merge.get("output") if isinstance(merge.get("output"), dict) else {}
        event = output.get("external_event") if isinstance(output.get("external_event"), dict) else {}
        return {**result, "outcome": "merged", "decided_by": str(output.get("merged_by") or event.get("merged_by") or "")}
    accepted = next((t for t in tasks if isinstance(t.get("input"), dict) and (t.get("input") or {}).get(ACCEPT_KEY)), None)
    if accepted is not None and status in {"succeeded", "superseded"}:
        actor = str(((accepted.get("input") or {}).get(ACCEPT_KEY) or {}).get("actor") or "")
        return {**result, "outcome": "accepted", "decided_by": _who(actor)}
    if status == "cancelled":
        import re

        match = re.search(r"\bby ([^\s,;:]+)", str(workflow.get("cancellation_reason") or ""))
        return {**result, "outcome": "cancelled", "decided_by": _who(match.group(1)) if match else ""}
    if status == "failed":
        return {**result, "outcome": "failed"}
    if status == "superseded":
        return {**result, "outcome": "replaced"}
    return result


def _repo(pr_url: str) -> str:
    parts = str(pr_url or "").split("/")
    return f"{parts[3]}/{parts[4]}".lower() if len(parts) > 4 and "github.com" in pr_url else ""


def period_runs(
    memory: Any,
    *,
    tenant_id: str = "default",
    allowed: Any = None,
    days: int = 14,
    include_running: bool = False,
    now: datetime | None = None,
) -> Any:
    """Ticket and work-order runs finished in the last ``days`` (and running ones, when asked),
    across the programs ``allowed`` lets the caller read: each with its program, the workflow
    record with its tasks, and how it ended (``outcome`` is ``running`` while it still runs)."""
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, min(int(days or 14), 365)))
    programs = (memory.list_programs(tenant_id=tenant_id, limit=200) or {}).get("programs") or []
    for program in programs:
        program_id = str(program.get("id") or "")
        updated = _parse(program.get("updated_at"))
        if not program_id or (updated and updated < cutoff):
            continue
        if allowed is not None and not allowed(program_id):
            continue
        project_key = str(program.get("project_id") or program.get("project_key") or "")
        label = str(program.get("display_name") or program.get("summary") or program_id)[:120]
        for record in _program_items(memory, program_id, running_only=False):
            running = str(record.get("status") or "") == "running"
            if running and not include_running:
                continue
            workflow = _workflow(memory, str(record["id"]))
            tasks = [t for t in workflow.get("tasks") or [] if isinstance(t, dict)]
            if running:
                ended = {"outcome": "running", "decided_by": "", "finished_at": ""}
            else:
                ended = outcome(workflow, tasks)
                finished = _parse(ended["finished_at"])
                if finished and finished < cutoff:
                    continue
            yield {"program_id": program_id, "program_label": label, "project": project_key,
                   "workflow": workflow, "tasks": tasks, "ended": ended}


def history(
    memory: Any,
    *,
    tenant_id: str = "default",
    allowed: Any = None,
    days: int = 14,
    project: str = "",
    repo: str = "",
    outcome_filter: str = "",
    q: str = "",
    limit: int = 200,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Finished tickets and work orders across programs, newest first, with filters and facets."""
    now = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for run in period_runs(memory, tenant_id=tenant_id, allowed=allowed, days=days, now=now):
        view = item_view(memory, run["workflow"], now=now)
        view.update(run["ended"])
        view.update({"program_id": run["program_id"], "program_label": run["program_label"], "project": run["project"],
                     "repo": _repo(view["pr_url"])})
        rows.append(view)
    facets = {
        "projects": sorted({r["project"] for r in rows if r["project"]}),
        "repos": sorted({r["repo"] for r in rows if r["repo"]}),
        "outcomes": [o for o in OUTCOMES if any(r["outcome"] == o for r in rows)],
    }
    needle = q.strip().lower()
    filtered = [
        r for r in rows
        if (not project or r["project"] == project)
        and (not repo or r["repo"] == repo.lower())
        and (not outcome_filter or r["outcome"] == outcome_filter)
        and (not needle or needle in " ".join([r["jira_issue_key"], r["label"], r["program_label"], r["repo"]]).lower())
    ]
    filtered.sort(key=lambda r: r["finished_at"], reverse=True)
    return {"generated_at": _iso(now), "days": int(days or 14), "items": filtered[:limit], "total": len(filtered), "facets": facets}
