"""Intake wait/resume helpers for channel-driven clarification loops."""

from __future__ import annotations

from typing import Any

INTAKE_RESPONSE_EVENT = "channel.intake_response"
AWAIT_INTAKE_STEP = "await_intake_answers"


def task_awaiting_intake_response(
    tasks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    prefix = f"awaiting_external:{INTAKE_RESPONSE_EVENT}"
    intake_succeeded = any(
        str(task.get("step_name") or "") == "intake"
        and str(task.get("status") or "").lower() == "succeeded"
        for task in tasks
    )
    for task in tasks:
        if str(task.get("step_name") or "") != AWAIT_INTAKE_STEP:
            continue
        status = str(task.get("status") or "").lower()
        if intake_succeeded and status in {"pending", "ready"}:
            return task
        if status != "blocked":
            continue
        error = str(task.get("error") or "")
        if error.startswith(prefix) or error.startswith(
            "wait_external setup failed:"
        ):
            return task
    return None


def task_has_external_wait(task: dict[str, Any]) -> bool:
    prefix = f"awaiting_external:{INTAKE_RESPONSE_EVENT}"
    return str(task.get("error") or "").startswith(prefix)


def _latest_intake_task(
    tasks: list[dict[str, Any]],
    intake_step: str,
) -> dict[str, Any] | None:
    matches = [
        task
        for task in tasks
        if task.get("step_name") == intake_step
        and task.get("status") == "succeeded"
    ]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda task: str(task.get("created_at") or ""),
    )[-1]


def intake_task_output(
    tasks: list[dict[str, Any]],
    *,
    intake_step: str = "intake",
) -> dict[str, Any]:
    latest = _latest_intake_task(tasks, intake_step)
    output = latest.get("output") if latest else None
    return dict(output) if isinstance(output, dict) else {}


def intake_original_request(
    tasks: list[dict[str, Any]],
    *,
    intake_step: str = "intake",
) -> str:
    """The requester's own words that started the workflow.

    ``input.summary`` is a short title, so the full text comes from the user
    turns in the transcript context the program passed to intake.
    """
    latest = _latest_intake_task(tasks, intake_step)
    task_input = latest.get("input") if latest else None
    if not isinstance(task_input, dict):
        return ""
    context = task_input.get("context")
    transcript = context.get("transcript") if isinstance(context, dict) else None
    user_turns = [
        str(message.get("content") or "").strip()
        for message in transcript or []
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    text = "\n\n".join(turn for turn in user_turns if turn)
    if text:
        return text
    fallback = task_input.get("summary")
    if not fallback and isinstance(context, dict):
        fallback = context.get("summary")
    return str(fallback or "").strip()


def merge_intake_response(
    intake_output: dict[str, Any],
    user_text: str,
    *,
    summary: str = "",
    original_request: str = "",
) -> dict[str, Any]:
    text = (user_text or "").strip()
    base_summary = str(
        intake_output.get("summary")
        or summary
        or intake_output.get("content")
        or ""
    ).strip()
    request = (original_request or "").strip()
    clarified = {
        "summary": base_summary,
        "initial_request": request or base_summary,
        "questions": intake_output.get("questions") or [],
        "user_response": text,
        "answers": text,
        "intake_output": dict(intake_output),
    }
    return {
        "external_event": {
            "user_text": text,
            "clarified_requirements": clarified,
        },
        "event_type": INTAKE_RESPONSE_EVENT,
        "clarified_requirements": clarified,
        "user_response": text,
    }
