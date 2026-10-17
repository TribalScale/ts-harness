"""Deadlines for external waits and the action an operator should take when one expires.

A wait escalates after ``escalate_after_seconds`` (default: ``timeout_seconds``) and
expires only when ``expires_after_seconds`` is set. Expiry never fails the task; it
records a recommended action and leaves the task blocked for a human.
"""

from __future__ import annotations

from typing import Any

RECOMMENDED_ACTIONS = {
    "github.merge": "review_and_merge_or_cancel_pr",
    "github.pr_checks": "fix_failing_or_stuck_checks",
    "github.deployment_status": "inspect_deployment_or_rollback",
    "workflow.child_terminal": "inspect_stuck_work_orders",
}
GATE_ACTION = "decide_or_reassign_expired_gate"
DEFAULT_ACTION = "resume_or_cancel_stalled_wait"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def wait_deadlines(config: dict[str, Any] | None) -> dict[str, int]:
    """``set_task_wait`` deadline kwargs for a ``wait_external`` stage config."""
    config = dict(config or {})
    out: dict[str, int] = {}
    escalate = _positive_int(config.get("escalate_after_seconds")) or _positive_int(config.get("timeout_seconds"))
    expires = _positive_int(config.get("expires_after_seconds"))
    if escalate:
        out["escalate_after_seconds"] = escalate
    if expires:
        out["expires_after_seconds"] = expires
    return out


def recommended_action(wait: dict[str, Any]) -> str:
    if wait.get("approval_required") and not wait.get("expected_event_type"):
        return GATE_ACTION
    return RECOMMENDED_ACTIONS.get(str(wait.get("expected_event_type") or ""), DEFAULT_ACTION)
