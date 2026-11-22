"""A ticket's story: what happened, in order, in plain words, with where the time went.

The Live page and the program board open this for one ticket or work order. Each coding run
shows its model, trigger, phases (setup, coding, tests, pre-push review, push), the pre-push
review's result and its cost; each gate shows its verdict and findings; each wait for a person
shows how long it lasted and who decided. A running coding job also gets a live breakdown of
its current phase, from the worker's heartbeat.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ts_harness.workflows import live_view as lv
from ts_harness.workflows import phase_activity as pa
from ts_harness.workflows.human_decisions import ACCEPT_KEY, STOPPED_PREFIX, display_actor
from ts_harness.workflows.work_order_waves import HUMAN_DECISION_KEY

GATE_STEPS = {"finalize_code_review": "Code review", "qa_gate": "QA"}
WAIT_REASONS = {
    "revision_limit": "the automatic revisions ran out",
    "conflicting_feedback": "code review and QA ask for opposite changes",
    "failed_run": "the coding run failed",
    "auto_accept": "triage accepted it with follow-ups",
}
MESSAGE_LIMIT = 600


def _parse(value: Any) -> datetime | None:
    return lv._parse(value)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _seconds(start: datetime | None, end: datetime | None) -> int | None:
    if start and end:
        return max(0, int((end - start).total_seconds()))
    return None


def _input(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("input") if isinstance(task.get("input"), dict) else {}


def _output(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("output") if isinstance(task.get("output"), dict) else {}


def _finding(item: Any) -> dict[str, Any] | None:
    """One review finding or QA defect, normalized for display."""
    if isinstance(item, str):
        text = item.strip()
        return {"severity": "", "category": "", "file": "", "line": 0, "message": text[:MESSAGE_LIMIT]} if text else None
    if not isinstance(item, dict):
        return None
    message = str(item.get("message") or item.get("description") or item.get("summary") or item.get("title") or "").strip()
    if not message:
        return None
    line = item.get("line")
    return {
        "severity": str(item.get("severity") or ""),
        "category": str(item.get("category") or ""),
        "file": str(item.get("file") or ""),
        "line": line if isinstance(line, int) else 0,
        "message": message[:MESSAGE_LIMIT],
    }


def _job_model(job: dict[str, Any]) -> str:
    if job.get("model"):
        return str(job["model"])
    usage = (job.get("result") or {}).get("usage") if isinstance(job.get("result"), dict) else None
    for row in usage or []:
        if isinstance(row, dict) and row.get("model"):
            return str(row["model"])
    return ""


def model_label(model: str) -> str:
    """``claude-opus-5-5`` reads as "Opus"; anything else as it is."""
    text = str(model or "").split("/")[-1].lower()
    for name in ("opus", "sonnet", "haiku", "fable"):
        if name in text:
            return name.capitalize()
    return model or "default model"


def _cost(job: dict[str, Any]) -> float | None:
    usage = (job.get("result") or {}).get("usage") if isinstance(job.get("result"), dict) else None
    costs = [float(row.get("cost_usd") or 0) for row in usage or [] if isinstance(row, dict)]
    return round(sum(costs), 2) if costs else None


TRIGGERS = ("code_review", "qa", "ci", "guided", "triage", "human", "rerun", "other")


def trigger_kind(task: dict[str, Any], revision: int) -> str:
    """Why a coding run happened: ``""`` for the first run, else one of ``TRIGGERS``."""
    data = _input(task)
    if revision == 0:
        return ""
    if data.get("_guided"):
        return "guided"
    if data.get("_auto_grant"):
        return "triage"
    if isinstance(data.get("_revision_grant"), dict):
        return "human"
    if data.get("_revision_budget") == "checks":
        return "ci"
    source = str(data.get("_revision_source") or "")
    if source == "finalize_code_review":
        return "code_review"
    if source == "qa_gate":
        return "qa"
    if data.get("qa_feedback") and not data.get("review_feedback"):
        return "rerun"
    return "other"


def _trigger(task: dict[str, Any], revision: int) -> str:
    kind = trigger_kind(task, revision)
    if kind == "human":
        grant = _input(task).get("_revision_grant") or {}
        return f"after /revise by {display_actor(str(grant.get('actor') or 'a person'))}"
    return {
        "": "",
        "guided": "guided: the same finding came back",
        "triage": "automatic, after triage",
        "ci": "CI failed",
        "code_review": f"{GATE_STEPS['finalize_code_review'].lower()} findings",
        "qa": f"{GATE_STEPS['qa_gate'].lower()} findings",
        "rerun": "re-run after a failed run",
    }.get(kind, "revision")


def _coding_events(task: dict[str, Any], revision: int, jobs: list[dict[str, Any]], bar: dict[str, Any]) -> list[dict[str, Any]]:
    title = "Coding started" if revision == 0 else f"Revision {revision}"
    trigger = _trigger(task, revision)
    # For delivery metrics: which coding step run (revision) and why it ran.
    ids = {"task_id": str(task.get("id") or ""), "revision": revision, "trigger_kind": trigger_kind(task, revision)}
    events: list[dict[str, Any]] = []
    if not jobs:
        events.append({
            **ids, "at": bar["start"], "kind": "coding", "title": title, "subtitle": trigger,
            "seconds": bar["seconds"], "status": "running" if bar["open"] else _status(task),
        })
        return events
    for index, job in enumerate(sorted(jobs, key=lambda j: str(j.get("created_at") or ""))):
        created, started, finished = _parse(job.get("created_at")), _parse(job.get("started_at")), _parse(job.get("finished_at"))
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        status = str(job.get("status") or "")
        event: dict[str, Any] = {
            **ids,
            "job_status": status,
            "at": _iso(started or created),
            "kind": "coding",
            "title": title if index == 0 else f"{title}, attempt {index + 1}",
            "subtitle": trigger,
            "model": model_label(_job_model(job)),
            "seconds": _seconds(started, finished),
            "queued_seconds": _seconds(created, started) or 0,
            "cost_usd": _cost(job),
            "phases": result.get("timings") if isinstance(result.get("timings"), dict) else None,
            "review": result.get("pre_push_review") if isinstance(result.get("pre_push_review"), dict) else None,
        }
        if status in {"queued", "starting", "running"}:
            event["status"] = "running"
            event["progress"] = job.get("progress") if isinstance(job.get("progress"), dict) else None
            event["started_at"] = _iso(started)
        elif status == "succeeded" and result.get("ok"):
            event["status"] = "ok"
            event["outcome"] = "Pushed to the PR" if revision else "Opened the PR"
        elif status == "lost":
            event["status"] = "failed"
            event["outcome"] = f"Lost: {str(job.get('error') or result.get('error') or '').replace('coding job lost: ', '')}"
        elif status == "cancelled":
            event["status"] = "failed"
            event["outcome"] = "Cancelled"
        else:
            event["status"] = "failed"
            event["outcome"] = str(result.get("error") or job.get("error") or "The run failed")[:300]
        events.append(event)
    return events


def _status(task: dict[str, Any]) -> str:
    status = str(task.get("status") or "")
    if status == "succeeded":
        return "ok"
    if status in {"failed", "cancelled"}:
        return "failed"
    if status == "blocked":
        return "blocked"
    return "running"


def _gate_event(task: dict[str, Any], bar: dict[str, Any]) -> dict[str, Any] | None:
    step = str(task.get("step_name") or "")
    name = GATE_STEPS[step]
    output = _output(task)
    status = str(task.get("status") or "")
    if step == "finalize_code_review":
        raw = output.get("findings") or []
    else:
        result = output.get("result") if isinstance(output.get("result"), dict) else output
        raw = [d for pr in result.get("prs") or [] if isinstance(pr, dict) for d in pr.get("defects") or []]
        raw = raw or list(result.get("defects") or [])
    findings = [f for f in (_finding(item) for item in raw) if f]
    if step == "finalize_code_review":
        from ts_harness.workflows.code_review import is_blocking

        blocking = [f for f in findings if is_blocking(f)]
        others = [f for f in findings if not is_blocking(f)]
    else:
        blocking, others = findings, []
    passed = status == "succeeded"
    if bar["open"] and not output:
        return {"at": bar["start"], "kind": "gate", "title": f"{name} running", "status": "running", "seconds": bar["seconds"]}
    if not output and status != "succeeded":
        return None
    count = len(blocking)
    return {
        # A blocked gate stays open until a revision cancels it; its verdict came at the start.
        "at": (bar["end"] if passed else bar["start"]) or bar["start"],
        "kind": "gate",
        "step": step,
        "title": f"{name} passed" if passed else f"{name} blocked the PR",
        "subtitle": "" if passed else f"{count} blocking finding{'s' if count != 1 else ''}" if step == "finalize_code_review"
        else f"{count} defect{'s' if count != 1 else ''}",
        "status": "ok" if passed else "blocked",
        # A blocked gate's open time is the wait for a person, shown as its own event.
        "seconds": bar["seconds"] if passed else None,
        "findings": blocking,
        "other_findings": len(others),
    }


def _ci_event(task: dict[str, Any], bar: dict[str, Any]) -> dict[str, Any]:
    output = _output(task)
    status = str(task.get("status") or "")
    if bar["open"] and status != "succeeded":
        error = str(task.get("error") or "")
        if error.startswith("awaiting_external"):
            return {"at": bar["start"], "kind": "ci", "title": "Waiting for CI", "status": "running", "seconds": bar["seconds"]}
    failed = [str(item) for item in output.get("failed_checks") or [] if item]
    if status == "succeeded" and not failed:
        return {"at": bar["end"] or bar["start"], "kind": "ci", "title": "CI passed", "status": "ok", "seconds": bar["seconds"]}
    return {
        "at": bar["start"], "kind": "ci", "title": "CI failed",
        "subtitle": ", ".join(failed[:5]), "status": "blocked", "seconds": bar["seconds"],
    }


def _decision_events(tasks: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Waits for a person and what they decided."""
    events: list[dict[str, Any]] = []
    for task in tasks:
        data = _input(task)
        grant = data.get("_revision_grant") if isinstance(data.get("_revision_grant"), dict) else None
        if grant and grant.get("at"):
            decided = _parse(grant.get("at"))
            # The wait began when the decision was asked for (recorded since this release), else
            # when the last step before the decision finished.
            since = _parse(grant.get("waiting_since"))
            if since is None and decided:
                finished = [
                    _parse(t.get("completed_at")) for t in tasks
                    if _parse(t.get("completed_at")) and _parse(t.get("completed_at")) <= decided
                ]
                since = max((d for d in finished if d), default=None)
            if since:
                events.append({"at": _iso(since), "kind": "wait", "title": "Waiting for you", "status": "done",
                               "seconds": _seconds(since, decided)})
            guidance = re.sub(r"^/revise\s+\S+\s*", "", str(grant.get("guidance") or "").strip(), flags=re.IGNORECASE)
            events.append({"at": _iso(decided), "kind": "decision", "title": f"/revise by {display_actor(str(grant.get('actor') or 'a person'))}",
                           "subtitle": (guidance[:280] + ("…" if len(guidance) > 280 else "")) if guidance else "",
                           "status": "ok"})
        accepted = data.get(ACCEPT_KEY) if isinstance(data.get(ACCEPT_KEY), dict) else None
        if accepted:
            events.append({"at": str(accepted.get("at") or task.get("updated_at") or ""), "kind": "decision",
                           "title": f"Accepted as it is by {display_actor(str(accepted.get('actor') or 'a person'))}", "status": "ok"})
        error = str(task.get("error") or "")
        if error.startswith(STOPPED_PREFIX):
            actor = error[len(STOPPED_PREFIX):].split(":", 1)[0].strip() or "a person"
            events.append({"at": str(task.get("updated_at") or ""), "kind": "decision", "title": f"Stopped by {actor}", "status": "failed"})
        waiting = data.get(HUMAN_DECISION_KEY) if isinstance(data.get(HUMAN_DECISION_KEY), dict) else None
        if waiting and str(task.get("status") or "") == "blocked" and not error.startswith("awaiting_"):
            since = _parse(waiting.get("since")) or _parse(task.get("updated_at"))
            reason = WAIT_REASONS.get(str(waiting.get("reason") or ""), "a decision is needed")
            events.append({"at": _iso(since), "kind": "wait", "title": "Waiting for you", "subtitle": reason,
                           "status": "running", "seconds": _seconds(since, now)})
    return events


def _merge_event(task: dict[str, Any], bar: dict[str, Any]) -> dict[str, Any] | None:
    status = str(task.get("status") or "")
    if status == "succeeded":
        return {"at": bar["end"], "kind": "done", "title": "Merged", "subtitle": "after waiting for you",
                "status": "ok", "seconds": bar["seconds"], "waited": True}
    if bar["open"]:
        return {"at": bar["start"], "kind": "wait", "title": "Ready to merge: waiting for you", "status": "running",
                "seconds": bar["seconds"]}
    return None


def current_run(events: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The running coding job's breakdown: phases done, the current one and how long it has run."""
    running = next((e for e in reversed(events) if e.get("kind") == "coding" and e.get("status") == "running"), None)
    if running is None:
        return None
    progress = running.get("progress") or {}
    started = _parse(running.get("started_at"))
    return {
        "title": running["title"],
        "model": running.get("model") or "",
        "started_at": running.get("started_at") or "",
        "seconds": _seconds(started, now),
        "queued": not started,
        "phase": str(progress.get("phase") or ""),
        "phase_seconds": progress.get("phase_seconds"),
        "detail": str(progress.get("detail") or ""),
        "done": progress.get("done") if isinstance(progress.get("done"), dict) else {},
    }


TOTAL_LABELS = {"coding": "Coding", "queue": "Queued for a coding slot", "ci": "CI checks",
                "review": "Code review", "qa": "QA", "you": "Waiting for you"}


def _totals(events: list[dict[str, Any]], bars: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Seconds by who the ticket was waiting on. A blocked gate's open time is the wait for a
    person, so review and QA count only the reviewer's own steps and verdicts."""
    totals = {key: 0 for key in TOTAL_LABELS}
    for event in events:
        seconds = int(event.get("seconds") or 0)
        if event.get("kind") == "coding":
            totals["coding"] += seconds
            totals["queue"] += int(event.get("queued_seconds") or 0)
        elif event.get("kind") == "ci":
            totals["ci"] += seconds
        elif event.get("kind") == "wait" or event.get("waited"):
            totals["you"] += seconds
    for bar in bars:
        task = by_id.get(bar["task_id"]) or {}
        if bar["step_name"] in {"review_precheck", "code_review"} or (
            bar["step_name"] == "finalize_code_review" and task.get("status") == "succeeded"
        ):
            totals["review"] += int(bar["seconds"] or 0)
        elif bar["step_name"] == "qa_gate" and task.get("status") in {"succeeded", "running", "claimed"}:
            totals["qa"] += int(bar["seconds"] or 0)
    return {key: value for key, value in totals.items() if value}


def ticket_story(memory: Any, workflow: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tasks = [t for t in workflow.get("tasks") or [] if isinstance(t, dict)]
    by_id = {str(t.get("id") or ""): t for t in tasks}
    bars = lv.step_timeline(tasks, now=now)
    workflow_id = str(workflow.get("id") or "")
    try:
        jobs = list(memory.list_coding_jobs(workflow_id=workflow_id, limit=100) or [])
    except Exception:  # noqa: BLE001 - the story still shows the steps
        jobs = []
    jobs_by_task: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        jobs_by_task.setdefault(str(job.get("task_id") or ""), []).append(job)

    coding_step = next((str(t.get("step_name") or "") for t in tasks if t.get("agent_id") == pa.CODING_AGENT), "")
    events: list[dict[str, Any]] = []
    revision = -1
    for bar in bars:
        task = by_id.get(bar["task_id"]) or {}
        step = bar["step_name"]
        if step == coding_step or task.get("agent_id") == pa.CODING_AGENT:
            revision += 1
            events.extend(_coding_events(task, revision, jobs_by_task.get(bar["task_id"], []), bar))
        elif step in GATE_STEPS:
            event = _gate_event(task, bar)
            if event:
                events.append(event)
        elif step == "await_pr_checks":
            events.append(_ci_event(task, bar))
        elif step == "await_human_merge":
            event = _merge_event(task, bar)
            if event:
                events.append(event)
        elif step == "close_ticket" and task.get("status") == "succeeded":
            events.append({"at": bar["end"], "kind": "done", "title": "Jira ticket closed", "status": "ok"})
    events.extend(_decision_events(tasks, now))
    events.sort(key=lambda e: str(e.get("at") or ""))
    started = min((_parse(b["start"]) for b in bars if b["start"]), default=None)
    totals = _totals(events, bars, by_id)
    total = sum(totals.values()) or 1
    return {
        "workflow_id": workflow_id,
        "generated_at": _iso(now),
        "started_at": _iso(started),
        "events": events,
        "current_run": current_run(events, now),
        "totals": [
            {"category": key, "label": TOTAL_LABELS.get(key, key),
             "seconds": seconds, "share": round(seconds / total, 3)}
            for key, seconds in sorted(totals.items(), key=lambda kv: -kv[1]) if seconds
        ],
        "cost_usd": round(sum(e.get("cost_usd") or 0 for e in events), 2),
    }
