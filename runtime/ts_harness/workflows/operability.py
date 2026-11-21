"""Stuck-workflow / operability status aggregation helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_DONE = {"succeeded", "skipped"}


class GateNotReady(ValueError):
    """An approval was asked for before the step it approves had finished."""


def unfinished_dependencies(task: dict[str, Any], tasks: list[dict[str, Any]]) -> list[str]:
    """Step names ``task`` depends on that have not finished; empty when it can be decided."""
    by_id = {str(item.get("id")): item for item in tasks if item.get("id")}
    waiting: list[str] = []
    for dep_id in task.get("depends_on") or []:
        dep = by_id.get(str(dep_id)) or {}
        if str(dep.get("status") or "").lower() not in _DONE:
            waiting.append(str(dep.get("step_name") or dep_id))
    return waiting


def gate_is_ready(task: dict[str, Any], tasks: list[dict[str, Any]]) -> bool:
    """A pending approval whose earlier steps have all finished, so a person can decide it now."""
    return (
        bool(task.get("approval_required"))
        and str(task.get("approval_status") or "").lower() == "pending"
        and str(task.get("status") or "").lower() in {"pending", "blocked", "running"}
        and not unfinished_dependencies(task, tasks)
    )


def require_gate_ready(task: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    """Raise ``GateNotReady`` when ``task`` approves a step that has not finished yet."""
    waiting = unfinished_dependencies(task, tasks)
    if waiting:
        step = str(task.get("step_name") or "this approval").replace("_", " ")
        raise GateNotReady(f"{step} is not ready yet: {', '.join(name.replace('_', ' ') for name in waiting)} has not finished.")


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _age_seconds(since: datetime | None, *, now: datetime | None = None) -> float | None:
    if since is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - since).total_seconds())


def _deadline_fields(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "escalated": bool(task.get("escalated_at")),
        "escalate_at": task.get("escalate_at"),
        "expires_at": task.get("expires_at"),
    }


def _expired_action(task: dict[str, Any]) -> str | None:
    task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
    action = str(task_input.get("_recommended_action") or "").strip()
    return action or None


def build_operability_status(
    *,
    workflow: dict[str, Any],
    tasks: list[dict[str, Any]],
    artifacts: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the canonical stuck-visibility payload for a workflow."""
    artifacts = dict(artifacts or {})
    current = now or datetime.now(timezone.utc)
    definition_version = str(
        workflow.get("definition_version")
        or artifacts.get("definition_version")
        or ""
    )
    by_status: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        by_status.setdefault(str(task.get("status") or "unknown"), []).append(task)

    active = None
    for status in ("running", "blocked", "pending"):
        bucket = by_status.get(status) or []
        if bucket:
            active = bucket[0]
            break

    active_step = str((active or {}).get("step_name") or "") or None
    active_status = str((active or {}).get("status") or workflow.get("status") or "unknown")

    waiting_on: dict[str, Any] | None = None
    last_error = None
    recommended_action = "none"

    # Approval wait
    for task in tasks:
        # Only a gate whose earlier steps have finished: a later gate is also pending, and
        # listing it would let a person approve work that has not been produced yet.
        if gate_is_ready(task, tasks):
            waiting_on = {
                "kind": "approval",
                "event_type": None,
                "since": task.get("updated_at") or task.get("created_at"),
                "age_seconds": _age_seconds(
                    _parse_dt(task.get("updated_at") or task.get("created_at")),
                    now=current,
                ),
                "match": {"task_id": task.get("id"), "step_name": task.get("step_name")},
                **_deadline_fields(task),
            }
            recommended_action = _expired_action(task) or "approve_or_reject_pending_gate"
            active_step = str(task.get("step_name") or active_step)
            active_status = "blocked"
            break

    # External wait
    if waiting_on is None:
        for task in tasks:
            error = str(task.get("error") or "")
            if task.get("status") == "blocked" and (
                error.startswith("awaiting_external:")
                or task.get("wait_token")
                or (task.get("input") or {}).get("_merge_expected")
            ):
                event_type = error.removeprefix("awaiting_external:") if error.startswith("awaiting_external:") else "github.merge"
                match = {}
                expected = (task.get("input") or {}).get("_merge_expected")
                if isinstance(expected, list):
                    match = {"pr_urls": expected}
                elif task.get("wait_match"):
                    match = dict(task.get("wait_match") or {})
                waiting_on = {
                    "kind": "external",
                    "event_type": event_type or "github.merge",
                    "since": task.get("updated_at") or task.get("created_at"),
                    "age_seconds": _age_seconds(
                        _parse_dt(task.get("updated_at") or task.get("created_at")),
                        now=current,
                    ),
                    "match": match,
                    **_deadline_fields(task),
                }
                recommended_action = _expired_action(task) or "wait_for_github_merge_or_resume_external_event"
                active_step = str(task.get("step_name") or active_step)
                active_status = "blocked"
                break

    # Blocked QA
    if waiting_on is None:
        for task in tasks:
            if task.get("step_name") == "qa_gate" and task.get("status") == "blocked":
                waiting_on = {
                    "kind": "agent",
                    "event_type": "qa_gate",
                    "since": task.get("updated_at") or task.get("created_at"),
                    "age_seconds": _age_seconds(
                        _parse_dt(task.get("updated_at") or task.get("created_at")),
                        now=current,
                    ),
                    "match": {"task_id": task.get("id")},
                }
                last_error = str(task.get("error") or "qa_gate blocked")
                recommended_action = "review_qa_evidence_and_retry_or_revise_delivery"
                active_step = "qa_gate"
                active_status = "blocked"
                break

    # Running agent
    if waiting_on is None and by_status.get("running"):
        task = by_status["running"][0]
        waiting_on = {
            "kind": "agent",
            "event_type": None,
            "since": task.get("started_at") or task.get("updated_at") or task.get("created_at"),
            "age_seconds": _age_seconds(
                _parse_dt(task.get("started_at") or task.get("updated_at") or task.get("created_at")),
                now=current,
            ),
            "match": {"task_id": task.get("id"), "agent_id": task.get("agent_id")},
        }
        recommended_action = "wait_for_agent_or_inspect_runtime_task"

    lease = None
    # Only a running task holds a lease. A blocked or pending task can keep the expiry of
    # its last run, which read as a stale lease on a step that was simply waiting.
    if active and str(active.get("status") or "") == "running":
        expires = _parse_dt(active.get("lock_expires_at"))
        if expires is not None:
            expired = expires <= current
            lease = {
                "expires_at": expires.isoformat(),
                "expired": expired,
            }
            if expired:
                recommended_action = "recover_stale_lease_via_workflow_runner"
                waiting_on = waiting_on or {
                    "kind": "agent",
                    "event_type": "stale_lease",
                    "since": active.get("updated_at"),
                    "age_seconds": _age_seconds(_parse_dt(active.get("updated_at")), now=current),
                    "match": {"task_id": active.get("id")},
                }

    if last_error is None:
        for task in reversed(tasks):
            if task.get("error"):
                last_error = str(task.get("error"))
                break
        if last_error is None:
            last_error = str(artifacts.get("failure_reason") or "") or None

    pr_urls = artifacts.get("pr_urls")
    if not isinstance(pr_urls, list):
        pr_url = artifacts.get("pr_url")
        pr_urls = [pr_url] if pr_url else []

    qa_status = artifacts.get("qa_status")
    if qa_status is None:
        for task in tasks:
            if task.get("step_name") == "qa_gate" and isinstance(task.get("output"), dict):
                qa_status = task["output"].get("status")
                break

    legacy = False
    if definition_version.startswith("1.0") or definition_version.startswith("1.1"):
        legacy = True
        if not any(task.get("step_name") == "qa_gate" for task in tasks):
            artifacts = {**artifacts, "legacy_no_qa_gate": True}

    if artifacts.get("compensation_status") == "triggered" and recommended_action == "none":
        recommended_action = "inspect_compensation_and_failed_stage"
    compensation = artifacts.get("compensation") if isinstance(artifacts.get("compensation"), dict) else None
    if compensation and compensation.get("failures") and recommended_action == "none":
        recommended_action = "retry_failed_cancel_compensation"

    return {
        "definition_version": definition_version or None,
        "active_step": active_step,
        "active_task_status": active_status,
        "waiting_on": waiting_on,
        "lease": lease,
        "pr_urls": pr_urls,
        "qa_status": qa_status,
        "last_error": last_error,
        "recommended_action": recommended_action,
        "legacy_no_qa_gate": bool(artifacts.get("legacy_no_qa_gate") or legacy),
        "compensation_status": artifacts.get("compensation_status"),
        "compensation": compensation,
        "failed_stage": artifacts.get("failed_stage"),
    }


_CHILD_KEYS = ("waiting_on", "last_error", "active_step")


_SETTLED = {"succeeded", "cancelled", "skipped", "superseded"}


def child_steps(child_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each running child workflow's current step and what it waits for."""
    current: dict[str, dict[str, Any]] = {}
    for task in child_tasks:
        workflow_id = str(task.get("workflow_id") or "")
        if workflow_id in current or str(task.get("status") or "") in _SETTLED:
            continue
        status = str(task.get("status") or "")
        error = str(task.get("error") or "")
        if status == "pending" and task.get("approval_required"):
            waiting_for = "approval"
        elif error.startswith("awaiting_external:"):
            waiting_for = error.split(":", 1)[1].split()[0]
        elif status == "blocked":
            waiting_for = "decision"
        else:
            waiting_for = status
        current[workflow_id] = {
            "workflow_id": workflow_id,
            "workflow_type": task.get("workflow_type"),
            "step": task.get("step_name"),
            "waiting_for": waiting_for,
            # The inbox approves a child's gate by this id (a release track's promote gates).
            "task_id": task.get("id"),
            "since": task.get("updated_at") or task.get("created_at"),
        }
    return list(current.values())


def _from_raw(status: dict[str, Any]) -> dict[str, Any]:
    tasks = status.pop("active_tasks", None)
    workflow = status.pop("active_workflow", None)
    child_tasks = status.pop("active_child_tasks", None)
    if not status.get("active_workflow_id") or (tasks is None and workflow is None):
        return {}
    artifacts = status.get("artifacts") if isinstance(status.get("artifacts"), dict) else {}
    derived = build_operability_status(workflow=dict(workflow or {}), tasks=list(tasks or []), artifacts=artifacts)
    waiting_on = derived.get("waiting_on") if isinstance(derived.get("waiting_on"), dict) else {}
    if waiting_on.get("event_type") == "workflow.child_terminal" and child_tasks:
        steps = child_steps(list(child_tasks))
        waiting_on = {**waiting_on, "children": steps}
        derived["waiting_on"] = waiting_on
        approvals = [step for step in steps if step["waiting_for"] == "approval"]
        if approvals:
            step = str(approvals[0]["step"] or "")
            derived["recommended_action"] = step if step.startswith("approve") else f"approve_{step}"
        elif any(step["waiting_for"] == "decision" for step in steps):
            derived["recommended_action"] = "decide_blocked_child_step"
    return derived


def with_operability(status: dict[str, Any] | None) -> dict[str, Any]:
    """Add the operability fields to a program status from the memory API, which returns raw tasks."""
    status = dict(status or {})
    status.update(_from_raw(status))
    children = status.get("children")
    if isinstance(children, list):
        enriched = []
        for child in children:
            child = dict(child) if isinstance(child, dict) else child
            if isinstance(child, dict):
                derived = _from_raw(child)
                child.pop("artifacts", None)
                child.update({key: derived[key] for key in _CHILD_KEYS if derived.get(key)})
            enriched.append(child)
        status["children"] = enriched
    return status
