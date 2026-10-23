"""Stuck-workflow / operability status aggregation helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


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
        if task.get("approval_required") and task.get("approval_status") == "pending":
            if task.get("status") in {"pending", "blocked", "running"}:
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
    if active:
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


def _from_raw(status: dict[str, Any]) -> dict[str, Any]:
    tasks = status.pop("active_tasks", None)
    workflow = status.pop("active_workflow", None)
    if not status.get("active_workflow_id") or (tasks is None and workflow is None):
        return {}
    artifacts = status.get("artifacts") if isinstance(status.get("artifacts"), dict) else {}
    return build_operability_status(workflow=dict(workflow or {}), tasks=list(tasks or []), artifacts=artifacts)


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
