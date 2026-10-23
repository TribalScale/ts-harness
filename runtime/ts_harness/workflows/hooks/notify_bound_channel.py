"""Notify bound Slack thread on workflow events."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext) -> dict[str, Any]:
    skeleton = ctx.skeleton
    notifications = dict(skeleton.notifications or {})
    if not notifications.get("bound_channel"):
        return {"status": "skipped"}

    allowed_events = set(notifications.get("events") or [])
    event = ctx.runner_event
    if allowed_events and event not in allowed_events:
        return {"status": "skipped", "reason": "event not in allowlist"}

    workflow = ctx.workflow or {}
    source_channel = str(workflow.get("source_channel") or "")
    if source_channel.count(":") < 2:
        return {"status": "skipped", "reason": "no binding"}

    _, channel_id, thread_ts = source_channel.split(":", 2)
    task = ctx.task or {}
    error = ctx.extras.get("error")

    try:
        from ts_harness.notifications.slack import SlackNotifier

        channel_text = str(ctx.extras.get("channel_text") or "").strip()
        if channel_text:
            SlackNotifier().post_thread_text(
                text=channel_text,
                channel=channel_id,
                thread_ts=thread_ts,
            )
            return {"status": "notified", "event": event, "mode": "channel_text"}

        step_name = str(task.get("step_name") or "")
        mapped = _map_event(event)
        notify_error = str(error) if error else None
        if mapped == "blocked" and notify_error:
            if "sending it back to OpenCode" not in notify_error:
                notify_error = (
                    f"Step `{step_name or 'unknown'}` is blocked and will not retry "
                    f"automatically.\n{notify_error}\n"
                    "Reply in this thread if you want me to revise inputs or restart "
                    "the stage."
                )

        SlackNotifier().notify_workflow_event(
            event=mapped,
            workflow_id=ctx.workflow_id,
            task_id=str(task.get("id") or ""),
            agent_id=str(task.get("agent_id") or ""),
            step_name=step_name,
            tenant_id=ctx.tenant_id,
            error=notify_error,
            channel=channel_id,
            thread_ts=thread_ts,
        )
        return {"status": "notified", "event": event}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _map_event(event: str) -> str:
    mapping = {
        "succeeded": "completed",
        "failed": "failed",
        "blocked": "blocked",
        "awaiting_approval": "blocked",
        "awaiting_external": "blocked",
    }
    return mapping.get(event, event)
