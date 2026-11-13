"""``gather_intake_sources``: the barrier step that reads a program's shared files once.

Barrier config:

- ``intake_sources``: optional expression for the pointer list (defaults to the
  ``intake_sources`` field carried on the first task's input).
- ``agent_id``: whose Google Workspace mailbox reads Drive (default ``pm-agent``).
- ``deadline_seconds``: time budget for the whole gather (default 120).

Output: ``sources``, ``sources_text``, ``skipped``. A source that cannot be read is a
record with ``error``; the step itself only fails when the pointer list is not a list.
"""

from __future__ import annotations

from typing import Any

from ts_harness.intake import connectors
from ts_harness.intake.resolve import resolve_intake_sources
from ts_harness.workflows.hooks._helpers import load_workflow_payload

DEFAULT_AGENT_ID = "pm-agent"
DEFAULT_DEADLINE_SECONDS = 120.0


def _pointers(task: dict[str, Any], config: dict[str, Any], workflow: dict[str, Any], tasks: list[dict[str, Any]]) -> Any:
    from ts_harness.workflows.bug_context import _input_extras
    from ts_harness.workflows.expressions import resolve_value

    extras = _input_extras(task, tasks)
    if config.get("intake_sources") is not None:
        value = resolve_value(config.get("intake_sources"), tasks=tasks, workflow=workflow, extras=extras)
    else:
        value = extras.get("intake_sources")
    return [] if value is None else value


def gather_intake_sources(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    pointers = _pointers(task, config, workflow, tasks)
    if not isinstance(pointers, list):
        raise ValueError("intake_sources must be a list")
    if not pointers:
        return {"sources": [], "sources_text": "", "skipped": []}
    tenant_id = str(workflow.get("tenant_id") or task.get("tenant_id") or "default")
    agent_id = str(config.get("agent_id") or DEFAULT_AGENT_ID)
    needs_drive = any(isinstance(p, dict) and p.get("kind") == "drive" and not p.get("error") for p in pointers)
    scope = {
        "tenant": tenant_id,
        "program": workflow.get("program_id"),
        "workflow": task.get("workflow_id"),
        "task": task.get("task_id") or task.get("id"),
        "agent": agent_id,
    }
    return resolve_intake_sources(
        pointers,
        download=connectors.build_download(),
        drive=connectors.build_drive(tenant_id=tenant_id, agent_id=agent_id) if needs_drive else None,
        describe_image=connectors.build_image_describer(
            agent_id=agent_id, scope={k: v for k, v in scope.items() if v}
        ),
        deadline_s=float(config.get("deadline_seconds") or DEFAULT_DEADLINE_SECONDS),
    )
