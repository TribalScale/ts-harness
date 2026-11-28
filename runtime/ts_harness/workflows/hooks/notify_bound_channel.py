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

    from ts_harness.gateway.workflow_presentation import bound_channel

    parsed = bound_channel(source_channel)
    if parsed is None:
        return {"status": "skipped", "reason": "no binding"}
    channel, channel_id, thread_ts = parsed
    task = ctx.task or {}
    error = ctx.extras.get("error")
    channel_text = str(ctx.extras.get("channel_text") or "").strip()
    step_name = str(task.get("step_name") or "")
    mapped = _map_event(event)
    if mapped != "blocked":
        forget_posted_block(ctx.workflow_id, str(task.get("id") or ""))
    notify_error = str(error) if error else None
    # Web threads check their own transcript below; other channels remember the last block.
    if (
        channel != "web"
        and mapped == "blocked"
        and notify_error
        and _already_posted(ctx.workflow_id, str(task.get("id") or ""), notify_error)
    ):
        return {"status": "skipped", "reason": "same block already posted"}
    if mapped == "blocked" and notify_error and "needs_answers:" in notify_error:
        # A hand-written ticket's readiness questions (jira/ticket_readiness.py): already a
        # complete message for a person, not a failure to wrap.
        notify_error = notify_error.split("needs_answers:", 1)[1].strip()
    elif mapped == "blocked" and notify_error and "sending it back to OpenCode" not in notify_error:
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


# The last block posted per (workflow, task). A step that is re-checked and blocks again for
# the same reason is not posted again; a different reason, or a block after it ran again, is.
_POSTED_BLOCKS: dict[tuple[str, str], str] = {}
_POSTED_LIMIT = 2000
_TRANSCRIPT_LOOKBACK = 40


def _already_posted(workflow_id: str, task_id: str, error: str) -> bool:
    key = (str(workflow_id or ""), str(task_id or ""))
    if not key[1]:
        return False
    if _POSTED_BLOCKS.get(key) == error:
        return True
    if len(_POSTED_BLOCKS) >= _POSTED_LIMIT:
        _POSTED_BLOCKS.pop(next(iter(_POSTED_BLOCKS)))
    _POSTED_BLOCKS[key] = error
    return False


def forget_posted_block(workflow_id: str, task_id: str) -> None:
    """The step ran again: its next block is news, even with the same reason."""
    _POSTED_BLOCKS.pop((str(workflow_id or ""), str(task_id or "")), None)


def _recently_in_transcript(session_id: str, message: str) -> bool:
    """The same assistant text was posted since the person last wrote (survives restarts)."""
    try:
        from ts_harness.agent.transcript import get_transcript_store

        recent = get_transcript_store().load_messages(session_id, limit=_TRANSCRIPT_LOOKBACK)
    except Exception:  # noqa: BLE001 - no lookback: post it
        return False
    text = message.strip()
    for item in reversed(recent or []):
        role = str(item.get("role") or "")
        if role == "user":
            return False  # the person spoke since: say it again (they may have asked for a retry)
        if role == "assistant" and str(item.get("content") or "").strip() == text:
            return True
    return False


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
    if _recently_in_transcript(session.session_id, message):
        return True  # posted already; a repeat of the same text adds nothing
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
