"""What a program stage is doing: its own steps and the workflows it started, in operator wording."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.human_decisions import STOPPED_PREFIX
from ts_harness.workflows.work_order_waves import awaiting_human_decision

STEP_LABELS = {
    "await_pr_checks": "PR checks",
    "review_precheck": "review precheck",
    "code_review": "code review",
    "finalize_code_review": "code review",
    "qa_gate": "QA",
    "ensure_fresh": "freshness check",
    "await_human_merge": "merge",
}
CODING_AGENT = "dev-agent"
ACTIVE = {"running", "claimed"}
EVENT_WAITS = {
    "github.merge": ("Ready to merge", "waiting"),
    "github.pr_checks": ("Waiting for PR checks", "waiting"),
    "workflow.child_terminal": ("Waiting for the workflows it started", "waiting"),
    "coding.finished": ("OpenCode implementing", "running"),
}
CODING_WAIT = "awaiting_external:coding.finished"
UPSTREAM_WAIT = "release.upstream_recorded"


def _humanize(value: str) -> str:
    return value.replace("_", " ").strip()


def _label(step: str, coding_step: str) -> str:
    if coding_step and step == coding_step:
        return "OpenCode"
    return STEP_LABELS.get(step, _humanize(step))


def _input(task: dict[str, Any]) -> dict[str, Any]:
    value = task.get("input")
    return value if isinstance(value, dict) else {}


def _output(task: dict[str, Any]) -> dict[str, Any]:
    value = task.get("output")
    return value if isinstance(value, dict) else {}


def _approval_pending(task: dict[str, Any]) -> bool:
    return (
        bool(task.get("approval_required"))
        and task.get("approval_status") == "pending"
        and task.get("status") in {"pending", "blocked", "running"}
    )


def _by_created(tasks: list[dict[str, Any]], *, reverse: bool = False) -> list[dict[str, Any]]:
    return sorted(tasks, key=lambda t: str(t.get("created_at") or ""), reverse=reverse)


def _latest_per_step(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    for task in _by_created(tasks):
        step = str(task.get("step_name") or "")
        if step not in latest:
            order.append(step)
        latest[step] = task
    return [latest[step] for step in order]


def _workflow_label(tasks: list[dict[str, Any]], workflow: dict[str, Any]) -> str:
    for task in tasks:
        work_order_id = str(_input(task).get("work_order_id") or "").strip()
        if work_order_id:
            return work_order_id
        track_repo = str(_input(task).get("track_repo") or "").strip()
        if track_repo:
            return track_repo
    return str(workflow.get("external_ref") or _humanize(str(workflow.get("workflow_type") or "")) or "workflow")


def _pr_url(tasks: list[dict[str, Any]]) -> str:
    for task in _by_created(tasks, reverse=True):
        url = _output(task).get("pr_url") or _input(task).get("pr_url")
        if url:
            return str(url)
    return ""


def _current(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first step that has not finished. A later approval gate is not current yet."""
    for task in steps:
        if task.get("status") not in {"succeeded", "cancelled", "superseded"}:
            return task
    return None


def _state(
    workflow_status: str,
    current: dict[str, Any] | None,
    *,
    coding_step: str,
    revisions: int,
    steps: list[dict[str, Any]],
) -> tuple[str, str]:
    stopped = next((t for t in steps if str(t.get("error") or "").startswith(STOPPED_PREFIX)), None)
    if stopped is not None:
        actor = str(stopped.get("error"))[len(STOPPED_PREFIX):].split(":", 1)[0].strip() or "a human"
        return f"Stopped by {actor}", "failed"
    if workflow_status == "succeeded":
        merged = any(t.get("step_name") == "await_human_merge" and t.get("status") == "succeeded" for t in steps)
        return ("Merged" if merged else "Done"), "done"
    if workflow_status == "superseded" and all(t.get("status") in {"succeeded", "cancelled"} for t in steps):
        return "Done", "done"
    if workflow_status in {"failed", "cancelled", "superseded"}:
        return workflow_status.capitalize(), "failed"
    if current is None:
        return "Done", "done"

    step = str(current.get("step_name") or "")
    label = _label(step, coding_step)
    status = str(current.get("status") or "")
    error = str(current.get("error") or "")
    if awaiting_human_decision(current):
        return f"Stuck at {label}: waiting for your decision", "stuck"
    if _approval_pending(current):
        return f"Waiting for approval: {label}", "waiting"
    if status in ACTIVE or (status == "blocked" and error == CODING_WAIT):
        if step == coding_step:
            if _input(current).get("refresh"):
                return "OpenCode refreshing the branch", "running"
            if revisions:
                return f"OpenCode revising (revision {revisions})", "running"
            return "OpenCode implementing", "running"
        return f"{label[:1].upper()}{label[1:]} running", "running"
    if status == "blocked":
        if error.startswith("awaiting_external:"):
            event = error.split(":", 1)[1]
            if event == UPSTREAM_WAIT:
                return _upstream_wait(current), "waiting"
            return EVENT_WAITS.get(event, (f"Waiting for {event}", "waiting"))
        return f"Blocked at {label}: {error[:120]}", "blocked"
    return f"Queued: {label}", "waiting"


def _upstream_wait(task: dict[str, Any]) -> str:
    """``Waits for api production``: the upstream repositories that have not released there yet."""
    options = _input(task).get("_wait_options")
    options = options if isinstance(options, dict) else {}
    pending = [str(repo).rsplit("/", 1)[-1] for repo in options.get("pending") or options.get("upstream") or []]
    environment = str(options.get("environment") or "").strip()
    if not pending:
        return f"Waiting for upstream releases to {environment}".strip()
    return f"Waits for {', '.join(pending)} {environment}".strip()


def summarize_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Summarize one workflow (``{id, status, workflow_type, tasks}``) for the program page."""
    tasks = [t for t in workflow.get("tasks") or [] if isinstance(t, dict)]
    ordered = _by_created(tasks)
    first = ordered[0] if ordered else {}
    coding_step = str(first.get("step_name") or "") if first.get("agent_id") == CODING_AGENT else ""
    revisions = max(0, sum(1 for t in tasks if t.get("step_name") == coding_step) - 1) if coding_step else 0
    steps = _latest_per_step(tasks)
    current = _current(steps)
    ready_gate = str(current.get("id") or "") if current and _approval_pending(current) else ""
    workflow_status = str(workflow.get("status") or "")
    state, kind = _state(workflow_status, current, coding_step=coding_step, revisions=revisions, steps=steps)
    return {
        "workflow_id": str(workflow.get("id") or ""),
        "workflow_type": str(workflow.get("workflow_type") or ""),
        "label": _workflow_label(tasks, workflow),
        "workflow_status": workflow_status,
        "pr_url": _pr_url(tasks),
        "current_step": str((current or {}).get("step_name") or ""),
        "state": state,
        "kind": kind,
        "revisions": revisions,
        "stuck_task_id": str(current.get("id")) if current and kind == "stuck" else "",
        "updated_at": max((str(t.get("updated_at") or "") for t in tasks), default=str(workflow.get("updated_at") or "")),
        "coding_job": None,
        "steps": [
            {
                "task_id": str(t.get("id") or ""),
                "step_name": str(t.get("step_name") or ""),
                "label": _label(str(t.get("step_name") or ""), coding_step),
                "agent_id": str(t.get("agent_id") or ""),
                "status": str(t.get("status") or ""),
                "approval_pending": bool(ready_gate) and str(t.get("id") or "") == ready_gate,
                "error": str(t.get("error") or "")[:300],
                "updated_at": str(t.get("updated_at") or ""),
            }
            for t in steps
        ],
    }


def _workflow_record(memory: Any, workflow_id: str) -> dict[str, Any]:
    record = memory.get_workflow(workflow_id) or {}
    workflow = dict(record.get("workflow") or {}) if isinstance(record.get("workflow"), dict) else dict(record)
    workflow.setdefault("id", workflow_id)
    workflow["tasks"] = list(record.get("tasks") or workflow.get("tasks") or [])
    return workflow


def _with_coding_job(memory: Any, summary: dict[str, Any]) -> dict[str, Any]:
    """Attach the running coding job's latest output line and start time to a parked step."""
    current = next((s for s in summary["steps"] if s["step_name"] == summary["current_step"]), None)
    if not current or current["status"] != "blocked" or current["error"] != CODING_WAIT:
        return summary
    try:
        jobs = memory.list_coding_jobs(task_id=current["task_id"], status="queued,starting,running", limit=1)
    except Exception:
        return summary
    if jobs:
        job = jobs[0]
        summary["coding_job"] = {
            "id": str(job.get("id") or ""),
            "status": str(job.get("status") or ""),
            "last_output": str(job.get("last_output") or "")[-300:],
            "started_at": str(job.get("started_at") or ""),
            "created_at": str(job.get("created_at") or ""),
        }
        if job.get("status") == "queued":
            summary["state"] = "OpenCode queued"
            summary["kind"] = "waiting"
    return summary


def phase_activity(memory: Any, program_id: str, phase: str) -> dict[str, Any]:
    """The stage's workflow summary and the workflows it started, sorted by label."""
    status = memory.get_program_status(program_id) or {}
    info = (status.get("phases") or {}).get(phase)
    info = info if isinstance(info, dict) else {}
    workflow_id = str(info.get("workflow_id") or "")
    result: dict[str, Any] = {
        "phase": phase,
        "state": str(info.get("state") or "not_started"),
        "workflow_id": workflow_id,
        "workflow": None,
        "children": [],
    }
    if not workflow_id:
        return result
    result["workflow"] = _with_coding_job(memory, summarize_workflow(_workflow_record(memory, workflow_id)))
    stage_workflows = {
        str(other.get("workflow_id") or "")
        for other in (status.get("phases") or {}).values()
        if isinstance(other, dict)
    }
    children = []
    for child in memory.get_workflow_children(workflow_id) or []:
        child_id = str(child.get("id") or "")
        if child_id and child_id not in stage_workflows and child.get("status") != "superseded":
            children.append(_with_coding_job(memory, summarize_workflow(_workflow_record(memory, child_id))))
    result["children"] = sorted(children, key=lambda item: (item["label"], item["workflow_id"]))
    return result
