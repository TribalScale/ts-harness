"""Resolve workflow skeleton input_map expression tokens."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_TOKEN = re.compile(r"^\$(.+)$")
_PRIOR_FIELD = re.compile(r"^prior\.(.+?)\.(output|input)(?:\.(.*))?$")


def _latest_succeeded(tasks: list[dict[str, Any]], step_names: list[str]) -> dict[str, Any] | None:
    candidates = [
        task
        for task in tasks
        if task.get("step_name") in step_names and task.get("status") == "succeeded"
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda task: str(task.get("created_at") or ""))[-1]


def _task_by_step(tasks: list[dict[str, Any]], step_name: str) -> dict[str, Any] | None:
    matches = [task for task in tasks if task.get("step_name") == step_name]
    if not matches:
        return None
    return sorted(matches, key=lambda task: str(task.get("created_at") or ""))[-1]


def _revision_count(tasks: list[dict[str, Any]], step_name: str) -> int:
    return sum(1 for task in tasks if task.get("step_name") == step_name) + 1


def _human_requests(tasks: list[dict[str, Any]], step_name: str) -> list[str]:
    """Feedback of every human-requested revision of ``step_name``, oldest first."""
    requests: list[str] = []
    for task in sorted(tasks, key=lambda item: str(item.get("created_at") or "")):
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        if task.get("step_name") != step_name or task.get("status") == "cancelled":
            continue
        feedback = str(task_input.get("review_feedback") or "").strip()
        if task_input.get("_revision_budget") == "human" and feedback:
            requests.append(feedback)
    return requests


def resolve_token(
    token: str,
    *,
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any] | None = None,
    reject_comments: str = "",
    source_steps: list[str] | None = None,
    extras: dict[str, Any] | None = None,
) -> Any:
    """Resolve a single $-prefixed expression token."""
    extras = extras or {}
    match = _TOKEN.match(str(token).strip())
    if not match:
        return token

    path = match.group(1)
    if path == "reject.comments":
        return reject_comments
    if path == "item":
        return extras.get("item")
    if path.startswith("item."):
        value = extras.get("item")
        for part in path[len("item.") :].split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value
    if path == "revise.id":
        revise = extras.get("revise") or {}
        return revise.get("id")
    if path == "re_gate.id":
        re_gate = extras.get("re_gate") or {}
        return re_gate.get("id")
    if path == "previous.id":
        previous = extras.get("previous") or {}
        return previous.get("id")
    if path == "source.output":
        source = _latest_succeeded(tasks, list(source_steps or []))
        return (source or {}).get("output")
    prior = _PRIOR_FIELD.match(path)
    if prior:
        step_name, section, nested = prior.group(1), prior.group(2), prior.group(3) or ""
        task = _task_by_step(tasks, step_name)
        value = (task or {}).get(section)
        for part in nested.split("."):
            if not part:
                continue
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value
    if path == "runtime.today":
        return datetime.now(timezone.utc).date().isoformat()
    if path.startswith("runtime.revision_count:"):
        step_name = path.split(":", 1)[1]
        return _revision_count(tasks, step_name)
    if path.startswith("runtime.human_requests:"):
        return _human_requests(tasks, path.split(":", 1)[1])
    if path == "workflow.binding_key":
        return (workflow or {}).get("source_channel")
    if path in {"workflow.approved_prd", "workflow.approved_artifact"}:
        if extras.get("approved_artifact") is not None:
            return extras.get("approved_artifact")
        if extras.get("approved_prd") is not None:
            return extras.get("approved_prd")
        for task in sorted(
            tasks,
            key=lambda item: str(item.get("created_at") or ""),
        ):
            task_input = task.get("input") or {}
            if isinstance(task_input, dict):
                if task_input.get("approved_artifact") is not None:
                    return task_input.get("approved_artifact")
                if task_input.get("approved_prd") is not None:
                    return task_input.get("approved_prd")
        return extras.get("approved_prd_artifact")
    if path == "workflow.work_order_id":
        return _workflow_handoff_value(
            tasks,
            extras,
            ("work_order_id", "next_work_order_id"),
        )
    if path == "workflow.work_order_ref":
        return _workflow_handoff_value(
            tasks,
            extras,
            ("work_order_ref", "next_work_order_id", "work_order_id"),
        )
    if path == "workflow.work_order_body":
        return _workflow_handoff_value(tasks, extras, ("work_order_body",))
    if path == "workflow.work_orders_markdown":
        return _workflow_handoff_value(tasks, extras, ("work_orders_markdown",))
    if path == "workflow.completed_work_order_ids":
        value = _workflow_handoff_value(tasks, extras, ("completed_work_order_ids",))
        return value if value is not None else []
    if path == "workflow.repo_url":
        return _workflow_handoff_value(tasks, extras, ("repo_url",))
    if path == "workflow.base_branch":
        return _workflow_handoff_value(tasks, extras, ("base_branch",))
    if path == "workflow.refinery_artifacts":
        return _workflow_handoff_value(tasks, extras, ("refinery_artifacts",))
    if path == "workflow.foundry_artifacts":
        return _workflow_handoff_value(tasks, extras, ("foundry_artifacts",))
    if path == "workflow.planner_artifacts":
        return _workflow_handoff_value(tasks, extras, ("planner_artifacts",))
    if path == "workflow.issue_type_by_project":
        return _workflow_handoff_value(
            tasks,
            extras,
            ("issue_type_by_project",),
            allow_empty=True,
        )
    if path in {
        "workflow.default_issue_type",
        "workflow.repo",
        "workflow.default_branch",
        "workflow.work_order_ids",
        "workflow.drive_library",
        "workflow.jira_epic_key",
        "workflow.bug_report",
        "workflow.project_repositories",
    }:
        return _workflow_handoff_value(tasks, extras, (path[len("workflow.") :],))
    if path.startswith("input."):
        return _workflow_handoff_value(tasks, extras, (path[len("input.") :],))
    return None


def _workflow_handoff_value(
    tasks: list[dict[str, Any]],
    extras: dict[str, Any],
    field_names: tuple[str, ...],
    *,
    allow_empty: bool = False,
) -> Any:
    for name in field_names:
        if extras.get(name) is not None:
            return extras.get(name)
    prep = _task_by_step(tasks, "prepare_work_order_delivery")
    if prep and prep.get("status") == "succeeded":
        output = prep.get("output") or {}
        if isinstance(output, dict):
            for name in field_names:
                value = output.get(name)
                if value is not None:
                    return value
    for task in sorted(tasks, key=lambda item: str(item.get("created_at") or "")):
        if task.get("status") != "succeeded":
            continue
        output = task.get("output") or {}
        if not isinstance(output, dict):
            continue
        for name in field_names:
            value = output.get(name)
            if value is not None:
                return value
    # Forked deliveries store the work order on the implement task input, not on a later stage.
    by_newest = sorted(tasks, key=lambda item: str(item.get("created_at") or ""), reverse=True)
    for task in sorted(by_newest, key=lambda item: item.get("step_name") != "deliver_work_order"):
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        for name in field_names:
            value = task_input.get(name)
            if allow_empty:
                if value is not None:
                    return value
            elif value not in (None, "", [], {}):
                return value
    return None


def resolve_input_map(
    input_map: dict[str, str],
    *,
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any] | None = None,
    reject_comments: str = "",
    source_steps: list[str] | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate an input_map dict, resolving string tokens to values."""
    resolved: dict[str, Any] = {}
    for key, expr in (input_map or {}).items():
        if isinstance(expr, str) and expr.startswith("$"):
            resolved[key] = resolve_token(
                expr,
                tasks=tasks,
                workflow=workflow,
                reject_comments=reject_comments,
                source_steps=source_steps,
                extras=extras,
            )
        else:
            resolved[key] = expr
    return resolved


def resolve_value(
    value: Any,
    *,
    tasks: list[dict[str, Any]],
    workflow: dict[str, Any] | None = None,
    reject_comments: str = "",
    source_steps: list[str] | None = None,
    extras: dict[str, Any] | None = None,
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return resolve_token(
            value,
            tasks=tasks,
            workflow=workflow,
            reject_comments=reject_comments,
            source_steps=source_steps,
            extras=extras,
        )
    if isinstance(value, list):
        return [
            resolve_value(
                item,
                tasks=tasks,
                workflow=workflow,
                reject_comments=reject_comments,
                source_steps=source_steps,
                extras=extras,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: resolve_value(
                item,
                tasks=tasks,
                workflow=workflow,
                reject_comments=reject_comments,
                source_steps=source_steps,
                extras=extras,
            )
            for key, item in value.items()
        }
    return value
