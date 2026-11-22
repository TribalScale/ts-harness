"""Finish block handling that a restart interrupted.

When a gate blocks (code review or QA rejects a PR), the runner marks the task blocked and
then runs the stage's ``on_block`` (``revise_on_reject``), which sends the work order back
or parks it for a person. If the runner restarts between the two (a redeploy), the task
stays blocked with neither a revision nor a decision request, and the ticket queue stops.

The runner's leader calls :func:`recover_unhandled_blocks` every minute. A blocked task is
recovered when its stage has a revise loop, it is not waiting for a person or an external
event, and it has been blocked for longer than the grace period (so the live handler is
never raced). Recovery replays the same transition the runner would have run.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

GRACE_SECONDS = 90
RETRY_AFTER_SECONDS = 1800
_attempted: dict[str, float] = {}


def _age_seconds(value: Any, now: datetime) -> float:
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


def unhandled(task: dict[str, Any], stage: Any, *, now: datetime, grace_seconds: float = GRACE_SECONDS) -> bool:
    """A blocked task whose revise loop never ran."""
    task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
    if task_input.get("_awaiting_human_decision"):
        return False
    error = str(task.get("error") or "")
    if not error or error.startswith(("awaiting_external:", "awaiting_tool_approval:", "stopped_by_human:")):
        return False
    on_block = dict(getattr(stage, "on_block", None) or {})
    if on_block.get("hook") != "revise_on_reject":
        return False
    return _age_seconds(task.get("updated_at"), now) >= grace_seconds


def recover_unhandled_blocks(
    memory: Any,
    engine: Any,
    *,
    tenant_id: str = "default",
    now: datetime | None = None,
    grace_seconds: float = GRACE_SECONDS,
) -> list[str]:
    """Replay ``on_block`` for each blocked task a restart left unhandled; returns their ids."""
    now = now or datetime.now(timezone.utc)
    clock = time.monotonic()
    recovered: list[str] = []
    for item in memory.list_stuck_tasks(tenant_id=tenant_id) or []:
        task_id = str(item.get("id") or "")
        workflow_id = str(item.get("workflow_id") or "")
        if not task_id or not workflow_id:
            continue
        if clock - _attempted.get(task_id, -RETRY_AFTER_SECONDS) < RETRY_AFTER_SECONDS:
            continue
        try:
            payload = engine.get_status(workflow_id)
            task = next((t for t in payload.get("tasks") or [] if str(t.get("id") or "") == task_id), None)
            if task is None or str(task.get("status") or "") != "blocked":
                continue
            skeleton = engine._skeleton_for_state(str(task.get("workflow_type") or item.get("workflow_type") or ""),
                                                  task=task, payload=payload)
            stage = skeleton.stage(str(task.get("step_name") or ""))
        except Exception as exc:  # noqa: BLE001 - one bad workflow must not stop the sweep
            logger.warning("block recovery: %s not inspected: %s", task_id, exc)
            continue
        if not unhandled(task, stage, now=now, grace_seconds=grace_seconds):
            continue
        _attempted[task_id] = clock
        try:
            engine.on_task_transition(
                task, "blocked", dispatch_result={"output": task.get("output") or {}}, error=str(task.get("error") or "")
            )
        except Exception:  # noqa: BLE001 - logged; retried after RETRY_AFTER_SECONDS
            logger.exception("block recovery: replay of %s failed", task_id)
            continue
        recovered.append(task_id)
        logger.info("block recovery: replayed on_block for %s (%s)", task_id, task.get("step_name"))
    return recovered


BOOTSTRAP_CHECK_SECONDS = 300
_bootstrap_checked: dict[str, float] = {}


def _merged(pr_url: str, github_for: Any) -> bool:
    from ts_harness.integrations.github_client import parse_github_pr_url

    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        return False
    pull = github_for(pr_url).get_pull(*parsed) or {}
    return bool(pull.get("merged"))


def resume_after_bootstrap(
    memory: Any,
    *,
    tenant_id: str = "default",
    github_for: Any = None,
    check_every: float = BOOTSTRAP_CHECK_SECONDS,
) -> list[str]:
    """Run again a step that waited for repositories' bootstrap PRs, once every one has merged.

    A ticket queue or ticket blocks with ``bootstrap_prs`` in its output until the starting-code
    PRs merge; it used to tell the person to start a new run. Checked every few minutes.
    """
    if github_for is None:
        from ts_harness.integrations.github_client import github_for as default_github_for

        github_for = default_github_for
    clock = time.monotonic()
    resumed: list[str] = []
    for item in memory.list_stuck_tasks(tenant_id=tenant_id) or []:
        task_id = str(item.get("id") or "")
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        prs = [str(url) for url in output.get("bootstrap_prs") or [] if url]
        if not task_id or not prs:
            continue
        if clock - _bootstrap_checked.get(task_id, -check_every) < check_every:
            continue
        _bootstrap_checked[task_id] = clock
        try:
            if not all(_merged(url, github_for) for url in prs):
                continue
            memory.resume_blocked_task(task_id, actor="workflow-runner", reason="bootstrap PRs merged")
        except Exception as exc:  # noqa: BLE001 - checked again on a later sweep
            logger.warning("bootstrap resume: %s not resumed: %s", task_id, exc)
            continue
        resumed.append(task_id)
        logger.info("bootstrap resume: %s runs again, its bootstrap PRs merged", task_id)
    return resumed
