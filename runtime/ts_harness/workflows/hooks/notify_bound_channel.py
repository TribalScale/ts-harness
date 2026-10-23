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

    from ts_harness.gateway.workflow_presentation import _parse_source_channel

    parsed = _parse_source_channel(source_channel)
    if parsed is None:
        return {"status": "skipped", "reason": "no binding"}
    channel, channel_id, thread_ts = parsed
    task = ctx.task or {}
    error = ctx.extras.get("error")
    channel_text = str(ctx.extras.get("channel_text") or "").strip()
    step_name = str(task.get("step_name") or "")
    mapped = _map_event(event)
    notify_error = str(error) if error else None
    if mapped == "blocked" and notify_error and "sending it back to OpenCode" not in notify_error:
        if not _is_wait_error(notify_error):
            notify_error = (
                f"Step `{step_name or 'unknown'}` is blocked and will not retry "
                f"automatically.\n{notify_error}\n"
                "Reply in this thread if you want me to revise inputs or restart "
                "the stage."
            )

    if channel == "web":
        message = _web_chat_message(
            event=event,
            channel_text=channel_text,
            error=notify_error,
        )
        if not message:
            return {"status": "skipped", "reason": "not a chat message"}
        if _append_web_transcript(ctx, source_channel, message):
            return {"status": "notified", "event": event, "mode": "web_transcript"}
        return {"status": "skipped", "reason": "no web session"}

    try:
        from ts_harness.notifications.slack import SlackNotifier

        if channel_text:
            SlackNotifier().post_thread_text(
                text=channel_text,
                channel=channel_id,
                thread_ts=thread_ts,
            )
            return {"status": "notified", "event": event, "mode": "channel_text"}

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


_WAIT_ERROR_PREFIXES = ("awaiting_external:", "awaiting_tool_approval:")
_WAIT_EVENTS = {"awaiting_external", "awaiting_approval"}


def _is_wait_error(error: str | None) -> bool:
    return str(error or "").startswith(_WAIT_ERROR_PREFIXES)


def _web_chat_message(*, event: str, channel_text: str, error: str | None) -> str:
    """User-facing web text. Status lines such as ``blocked — intake (id)`` stay off the chat."""
    if channel_text:
        return channel_text
    if event in _WAIT_EVENTS or _is_wait_error(error):
        return ""
    return str(error or "").strip()


def _append_web_transcript(ctx: HookContext, source_channel: str, message: str) -> bool:
    from ts_harness.agent.transcript import get_transcript_store
    from ts_harness.gateway.workflow_presentation import _session_for_workflow

    session = _session_for_workflow(
        ctx.workflow_id,
        ctx.tenant_id,
        source_channel,
        program_id=str((ctx.workflow or {}).get("program_id") or ""),
    )
    if session is None or not message.strip():
        return False
    get_transcript_store().append_messages(
        session.session_id,
        [{"role": "assistant", "content": message}],
    )
    return True


def _map_event(event: str) -> str:
    mapping = {
        "succeeded": "completed",
        "failed": "failed",
        "blocked": "blocked",
        "awaiting_approval": "blocked",
        "awaiting_external": "blocked",
    }
    return mapping.get(event, event)
