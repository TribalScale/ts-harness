"""Block task awaiting external event; resume via API.

Supports two matching modes:

* ``match`` – exact key/value rules (single PR merge, channel replies, ...).
* ``match_all`` – wait for *every* PR URL in a resolved set to be merged. Each
  received ``github.merge`` event is recorded idempotently on the wait task's
  input (``_merge_received``); the task only resumes once the received set
  covers the expected set. Work orders are a 1-item set; Jira aggregates many.
"""

from __future__ import annotations

import uuid
from typing import Any

from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.registry import HookContext
from ts_harness.workflows.wait_deadlines import wait_deadlines


def resolve_match_all_urls(
    match_all: dict[str, Any],
    *,
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any],
) -> list[str]:
    """Resolve the expected PR URL set for a ``match_all`` wait config."""
    source = match_all.get("source")
    item_key = str(match_all.get("item_key") or "").strip()
    resolved = resolve_value(source, tasks=tasks, workflow=workflow)
    urls: set[str] = set()
    if isinstance(resolved, (list, tuple)):
        for item in resolved:
            value: Any
            if item_key and isinstance(item, dict):
                value = item.get(item_key)
            else:
                value = item
            if isinstance(value, str) and value.strip():
                urls.add(value.strip())
    elif isinstance(resolved, str) and resolved.strip():
        urls.add(resolved.strip())
    return sorted(urls)


def _received_urls(task: dict[str, Any]) -> list[str]:
    task_input = task.get("input") or {}
    received = task_input.get("_merge_received") if isinstance(task_input, dict) else None
    if isinstance(received, list):
        return [str(item) for item in received if isinstance(item, str)]
    return []


def handle(ctx: HookContext) -> dict[str, Any]:
    if ctx.task is None:
        raise ValueError("task required for wait_external")

    config = dict(ctx.stage.wait_external or {})
    event_type = str(config.get("event_type") or "external.event")
    wait_token = str(uuid.uuid4())

    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    match_all_cfg = dict(config.get("match_all") or {})
    resolved_match: dict[str, Any] = {}
    expected_urls: list[str] = []
    if match_all_cfg:
        expected_urls = resolve_match_all_urls(
            match_all_cfg, tasks=tasks, workflow=workflow
        )
        resolved_match = {"match_all": expected_urls}
        # Seed the received set so partial merges accumulate idempotently.
        merged_input = {
            **(ctx.task.get("input") or {}),
            "_merge_received": _received_urls(ctx.task),
        }
        try:
            ctx.memory_client.patch_task_input(
                str(ctx.task["id"]), merged_input, actor=ctx.actor
            )
        except Exception:
            pass
    else:
        match_rules = dict(config.get("match") or {})
        resolved_match = {
            key: resolve_value(value, tasks=tasks, workflow=workflow)
            for key, value in match_rules.items()
        }
        if resolved_match:
            try:
                ctx.memory_client.patch_task_input(
                    str(ctx.task["id"]),
                    {**(ctx.task.get("input") or {}), "_wait_match": resolved_match},
                    actor=ctx.actor,
                )
            except Exception:
                pass

    ctx.memory_client.set_task_wait(
        str(ctx.task["id"]),
        wait_token=wait_token,
        expected_event_type=event_type,
        actor=ctx.actor or "workflow-runner",
        **wait_deadlines(config),
    )
    ctx.memory_client.block_task_api(
        str(ctx.task["id"]),
        f"awaiting_external:{event_type}",
        actor=ctx.actor or "workflow-runner",
    )

    return {
        "status": "awaiting_external",
        "wait_token": wait_token,
        "event_type": event_type,
        "match": resolved_match,
        "expected_pr_urls": expected_urls,
    }


def _normalize_merge_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize github.merge payload aliases into stable task-output keys."""
    out: dict[str, Any] = {}
    pr_url = (
        payload.get("pr_url")
        or payload.get("html_url")
        or payload.get("pull_request_url")
    )
    if pr_url:
        out["pr_url"] = str(pr_url)
    merge_sha = (
        payload.get("merge_sha")
        or payload.get("merge_commit_sha")
        or payload.get("sha")
        or payload.get("merge_commit")
    )
    if merge_sha:
        out["merge_sha"] = str(merge_sha)
    return out


def resume(ctx: HookContext) -> dict[str, Any]:
    event_type = str(ctx.extras.get("event_type") or "")
    payload = dict(ctx.extras.get("payload") or {})
    workflow, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)

    waiting = [
        task
        for task in tasks
        if task.get("status") == "blocked"
        and str(task.get("error") or "").startswith("awaiting_external:")
    ]
    if not waiting:
        raise ValueError("no tasks awaiting external event")

    for task in waiting:
        expected = str(task.get("expected_event_type") or "")
        if expected and expected != event_type:
            continue
        stage = ctx.skeleton.stage(str(task.get("step_name")))
        config = dict(stage.wait_external or {})
        match_all_cfg = dict(config.get("match_all") or {})

        if config.get("children"):
            return _resume_children(ctx, task=task, event_type=event_type, payload=payload)

        if match_all_cfg:
            result = _resume_match_all(
                ctx,
                task=task,
                match_all_cfg=match_all_cfg,
                tasks=tasks,
                workflow=workflow,
                event_type=event_type,
                payload=payload,
            )
            if result is None:
                continue
            return result

        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        pinned = task_input.get("_wait_match")
        match_rules = dict(pinned) if isinstance(pinned, dict) and pinned else dict(config.get("match") or {})
        if not _payload_matches(match_rules, payload, tasks, workflow):
            continue

        if stage.approval_required:
            return ctx.memory_client.approve_task(
                str(task["id"]),
                actor=ctx.actor,
            )
        task_output = ctx.extras.get("task_output")
        if isinstance(task_output, dict) and task_output:
            output = dict(task_output)
        else:
            output = {"external_event": payload, "event_type": event_type}
        if event_type == "github.merge":
            output.update(_normalize_merge_fields(payload))
            # Keep aliases available for consumers that read the raw payload.
            for key, value in _normalize_merge_fields(payload).items():
                payload.setdefault(key, value)
        if str(output.get("checks_state") or "") == "failure":
            failed = [str(item) for item in output.get("failed_checks") or [] if item]
            detail = ", ".join(failed) or "checks_state=failure"
            reason = f"external_blocked:{event_type} {detail}"
            ctx.memory_client.block_task_api(str(task["id"]), reason, output=output, actor=ctx.actor)
            return {"status": "blocked", "task_id": str(task["id"]), "reason": reason}
        blocked_by = _block_when_match(dict(config.get("block_when") or {}), output)
        if blocked_by:
            reason = f"external_blocked:{event_type} {blocked_by}"
            ctx.memory_client.block_task_api(str(task["id"]), reason, output=output, actor=ctx.actor)
            return {"status": "blocked", "task_id": str(task["id"]), "reason": reason}
        return ctx.memory_client.resume_external(
            ctx.workflow_id,
            event_type=event_type,
            payload=payload,
            actor=ctx.actor,
            task_output=output,
        )

    raise ValueError("no matching external event for workflow")


def _resume_children(
    ctx: HookContext,
    *,
    task: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Join a wave: resume once every pinned child finished; block if any failed."""
    from ts_harness.workflows.work_order_waves import pinned_children, wave_result

    children = pinned_children(task)
    if not children:
        raise ValueError("join has no pinned child workflows")
    aggregate = wave_result(ctx.memory_client, children)
    if aggregate["state"] == "pending":
        return {"status": "awaiting_more_children", "task_id": str(task["id"]), "pending": aggregate["pending"]}
    output = {**aggregate["output"], "event_type": event_type, "external_event": payload}
    if aggregate["state"] == "failed":
        failed_ids = [item for item in aggregate["output"]["failed_work_order_ids"] if item]
        if failed_ids:
            reason = f"external_blocked:{event_type} wave work orders failed: {', '.join(failed_ids)}"
        else:
            reason = f"external_blocked:{event_type} child workflows failed: {', '.join(aggregate['output']['failed_labels'])}"
        details = str(aggregate["output"].get("failure_details") or "").strip()
        if details:
            reason = f"{reason}\n{details}"
        ctx.memory_client.block_task_api(str(task["id"]), reason, output=output, actor=ctx.actor)
        return {"status": "blocked", "task_id": str(task["id"]), "reason": reason}
    return ctx.memory_client.resume_external(
        ctx.workflow_id,
        event_type=event_type,
        payload=payload,
        actor=ctx.actor,
        task_output=output,
    )


def _resume_match_all(
    ctx: HookContext,
    *,
    task: dict[str, Any],
    match_all_cfg: dict[str, Any],
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Record one PR merge; complete only when the whole set is merged.

    Returns ``None`` when this wait task does not own the incoming PR URL, so
    the caller keeps scanning other blocked waits.
    """
    expected = resolve_match_all_urls(match_all_cfg, tasks=tasks, workflow=workflow)
    normalized = _normalize_merge_fields(payload)
    incoming = str(normalized.get("pr_url") or payload.get("pr_url") or "").strip()
    if not expected or not incoming or incoming not in expected:
        return None

    received = sorted(set(_received_urls(task)) | {incoming})
    remaining = sorted(set(expected) - set(received))

    if remaining:
        merged_input = {**(task.get("input") or {}), "_merge_received": received}
        ctx.memory_client.patch_task_input(
            str(task["id"]), merged_input, actor=ctx.actor
        )
        return {
            "status": "awaiting_more_merges",
            "task_id": str(task["id"]),
            "received": received,
            "remaining": remaining,
        }

    for key, value in normalized.items():
        payload.setdefault(key, value)
    output = {
        "event_type": event_type,
        "external_event": payload,
        "pr_urls": expected,
        "pr_url": expected[0],
        "merged_pr_urls": received,
    }
    if normalized.get("merge_sha"):
        output["merge_sha"] = normalized["merge_sha"]
    return ctx.memory_client.resume_external(
        ctx.workflow_id,
        event_type=event_type,
        payload=payload,
        actor=ctx.actor,
        task_output=output,
    )


def _block_when_match(block_when: dict[str, Any], output: dict[str, Any]) -> str:
    """``key=value`` of the first ``block_when`` rule the event output hits, else ``""``."""
    for key, values in block_when.items():
        allowed = values if isinstance(values, (list, tuple)) else [values]
        actual = output.get(key)
        if actual is not None and str(actual) in {str(value) for value in allowed}:
            return f"{key}={actual}"
    return ""


def _payload_matches(
    match_rules: dict[str, Any],
    payload: dict[str, Any],
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any],
) -> bool:
    if not match_rules:
        return True
    for key, expected in match_rules.items():
        if isinstance(expected, str) and expected.startswith("$"):
            resolved = resolve_value(expected, tasks=tasks, workflow=workflow)
        else:
            resolved = expected
        if payload.get(key) != resolved and str(payload.get(key)) != str(resolved):
            return False
    return True
