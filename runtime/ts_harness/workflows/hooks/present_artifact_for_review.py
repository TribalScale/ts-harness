"""Post specialist task output to the bound channel and orchestrator session."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ts_harness.workflows.artifacts import artifact_from_dispatch
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.presentation import resolve_present_prompt
from ts_harness.workflows.registry import HookContext


def _runtime_present_specialist_output(
    *,
    workflow_id: str,
    workflow_type: str,
    tenant_id: str,
    step_name: str,
    agent_id: str,
    artifact: dict[str, Any],
    source_channel: str,
    present_prompt: str,
    actor: str,
    requester_actor_id: str = "",
    program_id: str = "",
    program_summary: str = "",
) -> dict[str, Any]:
    runtime_url = (
        os.environ.get("AGENT_RUNTIME_URL")
        or "http://127.0.0.1:18789"
    ).rstrip("/")
    token = os.environ.get("GATEWAY_TOKEN", "")
    if not token:
        return {"status": "error", "error": "GATEWAY_TOKEN is required"}

    body = json.dumps(
        {
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "tenant_id": tenant_id,
            "step_name": step_name,
            "agent_id": agent_id,
            "artifact": artifact,
            "source_channel": source_channel,
            "present_prompt": present_prompt,
            "actor_id": actor,
            "requester_actor_id": requester_actor_id or actor,
            "program_id": program_id,
            "program_summary": program_summary,
        }
    ).encode()
    request = urllib.request.Request(
        f"{runtime_url}/workflow/present-artifact",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode() or str(exc)
        return {"status": "error", "error": detail}
    except urllib.error.URLError as exc:
        return {"status": "error", "error": str(exc)}

    return json.loads(raw) if raw else {"status": "error", "error": "empty response"}


def handle(ctx: HookContext) -> dict[str, Any]:
    artifact = artifact_from_dispatch(ctx.dispatch_result, ctx.task)
    if not artifact:
        return {
            "hook": "present_specialist_output",
            "status": "skipped",
            "reason": "no specialist output",
        }

    workflow = ctx.workflow or {}
    if not workflow:
        workflow, _ = load_workflow_payload(ctx.memory_client, ctx.workflow_id)

    source_channel = str(workflow.get("source_channel") or "")
    from ts_harness.gateway.workflow_presentation import _parse_source_channel

    if _parse_source_channel(source_channel) is None:
        return {
            "hook": "present_specialist_output",
            "status": "skipped",
            "reason": "no bound channel",
        }

    task = ctx.task or {}
    step_name = str(task.get("step_name") or "")
    agent_id = str(task.get("agent_id") or getattr(ctx.stage, "agent_id", "") or "")
    present_prompt = resolve_present_prompt(
        ctx.stage,
        ctx.skeleton,
        ctx.tasks,
        presenting_step_name=step_name,
    )

    program_id = str(workflow.get("program_id") or "")
    program_summary = ""
    if program_id:
        try:
            program_payload = ctx.memory_client.get_program(program_id)
            program_summary = str((program_payload.get("program") or {}).get("summary") or "")
        except Exception:
            program_summary = ""

    result = _runtime_present_specialist_output(
        workflow_id=ctx.workflow_id,
        workflow_type=ctx.workflow_type,
        tenant_id=ctx.tenant_id,
        step_name=step_name,
        agent_id=agent_id,
        artifact=artifact,
        source_channel=source_channel,
        present_prompt=present_prompt,
        actor=ctx.actor,
        requester_actor_id=str(workflow.get("requester_actor_id") or ""),
        program_id=program_id,
        program_summary=program_summary,
    )
    return {"hook": "present_specialist_output", **result}
