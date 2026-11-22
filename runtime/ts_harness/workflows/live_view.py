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
                   "merge": "Waiting for merge", "decision": "Waiting for a decision", "other": "Other"}
COLUMNS = ("waiting_on_blockers", "ready", "coding", "in_review", "waiting_for_you", "done", "stopped")


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
        start = ready or claimed or _parse(task.get("created_at"))
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
        "elapsed_seconds": int((now - started).total_seconds()) if started else 0,
        "stronger_model": automatic > 0,
        "timeline": bars,
    }


def _workflow(memory: Any, workflow_id: str) -> dict[str, Any]:
    return pa._workflow_record(memory, workflow_id)


def _descendants(memory: Any, workflow_id: str, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 3:
        return []
    found = []
    for child in memory.get_workflow_children(workflow_id) or []:
        child_id = str(child.get("id") or "")
        if not child_id:
            continue
        found.append(child)
        found.extend(_descendants(memory, child_id, depth + 1))
    return found


def _program_items(memory: Any, program_id: str, *, running_only: bool) -> list[dict[str, Any]]:
    """The program's ticket and work-order workflows (its own and those its phases started)."""
    workflows = (memory.get_program(program_id) or {}).get("workflows") or []
    seen: dict[str, dict[str, Any]] = {}
    for record in workflows:
        wid = str(record.get("id") or "")
        candidates = [record, *_descendants(memory, wid)]
        for candidate in candidates:
            cid = str(candidate.get("id") or "")
            if not cid or str(candidate.get("workflow_type") or "") not in TICKET_TYPES:
                continue
            if running_only and str(candidate.get("status") or "") != "running":
                continue
            seen[cid] = candidate
    return list(seen.values())


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
    now = now or datetime.now(timezone.utc)
    programs = (memory.list_programs(tenant_id=tenant_id, status="running", limit=100) or {}).get("programs") or []
    items = []
    for program in programs:
        program_id = str(program.get("id") or "")
        if not program_id or (allowed is not None and not allowed(program_id)):
            continue
        for record in _program_items(memory, program_id, running_only=True):
            view = item_view(memory, _workflow(memory, str(record["id"])), now=now)
            view.update({"program_id": program_id,
                         "program_label": str(program.get("display_name") or program.get("summary") or program_id)[:120]})
            items.append(view)
    order = {"you": 0, "agent": 1, "ci": 2, "queue": 3, "done": 4}
    items.sort(key=lambda item: (order.get(item["waiting_on"], 5), -item["in_step_seconds"]))
    return {"generated_at": _iso(now), "items": items, "capacity": coding_capacity(memory)}


def _column(view: dict[str, Any] | None, *, done: bool, blocked_by: list[str]) -> str:
    if done:
        return "done"
    if view is None:
        return "waiting_on_blockers" if blocked_by else "ready"
    if view["kind"] == "done":
        return "done"
    if view["kind"] == "failed":
        return "stopped"
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
    for record in sorted(_program_items(memory, program_id, running_only=False), key=lambda r: str(r.get("created_at") or "")):
        view = item_view(memory, _workflow(memory, str(record["id"])), now=now)
        label = view["jira_issue_key"] or view["label"]
        if label not in keys:
            keys.append(label)
        views[label] = view  # the latest attempt wins
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
        for bar in (view or {}).get("timeline") or []:
            totals[bar["category"]] = totals.get(bar["category"], 0) + bar["seconds"]
        items.append({"key": key, "title": titles.get(key, (view or {}).get("label", "")), "column": column,
                      "blocked_by": blockers.get(key, []), "item": view})
    total = sum(totals.values()) or 1
    return {
        "generated_at": _iso(now),
        "program_id": program_id,
        "columns": list(COLUMNS),
        "items": items,
        "totals": [{"category": c, "label": CATEGORY_LABELS.get(c, c), "seconds": s, "share": round(s / total, 3)}
                   for c, s in sorted(totals.items(), key=lambda kv: -kv[1])],
        "parallel_limit": artifacts.get("max_parallel_tickets"),
    }
