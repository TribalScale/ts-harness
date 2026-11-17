"""Keep the confirmed ticket list equal to the list the barrier froze."""

from __future__ import annotations

from typing import Any

from ts_harness.workflows.registry import HookContext


def _barrier_output(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    for task in tasks:
        if str(task.get("step_name") or "") != "prepare_ticket_queue":
            continue
        if str(task.get("status") or "") not in {"succeeded", ""}:
            continue
        output = task.get("output")
        if isinstance(output, dict) and output.get("queue") is True:
            return output
    return {}


def handle(ctx: HookContext) -> dict[str, Any]:
    output: dict[str, Any] = {}
    task_output = (ctx.task or {}).get("output")
    if isinstance(task_output, dict):
        output.update(task_output)
    dispatch = ctx.dispatch_result or {}
    if isinstance(dispatch.get("output"), dict):
        output.update(dispatch["output"])

    tasks = list(ctx.tasks or [])
    if not tasks and ctx.memory_client is not None and ctx.workflow_id:
        from ts_harness.workflows.hooks._helpers import load_workflow_payload

        _, tasks = load_workflow_payload(ctx.memory_client, ctx.workflow_id)
    frozen = _barrier_output(tasks)
    keys = frozen.get("jira_issue_keys")
    if not isinstance(keys, list) or not keys:
        return {"status": "skipped", "output": output}
    if not all(isinstance(item, str) and item.strip() for item in keys):
        return {"status": "skipped", "output": output}
    cleaned = [item.strip() for item in keys]
    output["jira_issue_keys"] = cleaned
    output["jira_issue_key"] = cleaned[0]
    exclusions = frozen.get("queue_exclusions")
    output["queue_exclusions"] = list(exclusions) if isinstance(exclusions, list) else []
    project = str(frozen.get("jira_project_key") or "").strip()
    if project:
        output["jira_project_key"] = project
    return {"status": "frozen", "output": output}
