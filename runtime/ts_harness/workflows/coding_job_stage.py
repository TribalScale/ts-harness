"""Run a deliver stage as a queued coding job instead of an agent turn.

The runner creates the job and parks the task on ``coding.finished``. When the
worker finishes, the memory API puts the task back to pending; the runner then
turns the job result into the stage output and checks it against the stage
contract exactly as the agent loop would.
"""

from __future__ import annotations

import os
from typing import Any

from ts_harness.implementation.coding_job import (
    binding_from_task_input,
    build_coding_request,
    coding_stage_output,
)
from ts_harness.workflows import stage_contracts

EVENT_TYPE = "coding.finished"
WAIT_ERROR = f"awaiting_external:{EVENT_TYPE}"
ACTIVE = frozenset({"queued", "starting", "running"})
TERMINAL = frozenset({"succeeded", "failed", "lost", "cancelled"})
TRANSPORT_ERROR_PREFIX = "transport_error:"


def enabled() -> bool:
    return os.environ.get("CODING_JOBS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def stage_spec(task: dict[str, Any], task_input: dict[str, Any], *, program_id: str | None) -> dict[str, Any]:
    """The job spec for a deliver task; raises ``CodingRequestError`` when it cannot run."""
    workflow_id = str(task.get("workflow_id") or "").strip() or None
    bound = binding_from_task_input(task_input)
    args = {
        **task_input,
        "mode": "refresh" if bound.get("refresh") else "implement",
        "workflow_id": workflow_id,
    }
    return build_coding_request(
        args,
        delivery_workflow_id=workflow_id,
        program_id=program_id,
        requested_by=str(task_input.get("requested_by") or "") or None,
        bound=bound,
        agent_id=str(task.get("agent_id") or "dev-agent"),
    )


def _synthetic_tool_audit(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Present the job result the way the agent loop records a run_coding_backend call."""
    return [{"tool": "run_coding_backend", "decision": "allow", "result_preview": run, "coding_result": run}]


def dispatch_result(
    job: dict[str, Any],
    contract: dict[str, Any] | None,
    task_input: dict[str, Any],
) -> dict[str, Any]:
    """Map a finished job to the runner's dispatch result (succeeded or blocked)."""
    job_id = str(job.get("id") or "")
    status = str(job.get("status") or "")
    run = job.get("result") if isinstance(job.get("result"), dict) else None
    if status == "lost":
        reason = (run or {}).get("error") or f"coding job lost: {job.get('error') or 'no result'}"
        return {"status": "blocked", "error": f"{TRANSPORT_ERROR_PREFIX} {reason}", "output": {}, "coding_job_id": job_id}
    if status == "cancelled":
        return {
            "status": "blocked",
            "error": f"coding job cancelled: {job.get('error') or 'cancelled'}",
            "output": {},
            "coding_job_id": job_id,
        }
    run = run or {"ok": False, "error": job.get("error") or "coding job returned no result"}
    output = coding_stage_output(run)
    if run.get("ok") is not True:
        error = f"deliver_work_order cannot succeed when run_coding_backend failed: {output.get('error')}"
        return {"status": "blocked", "error": error, "output": output, "coding_job_id": job_id}
    if not contract:
        return {"status": "succeeded", "output": output, "coding_job_id": job_id}

    tool_audit = _synthetic_tool_audit(run)
    violations = stage_contracts.validate_stage_output(contract, output, task_input, tool_audit)
    if violations:
        error = "; ".join(f"{v.path or '/'}: {v.message}" for v in violations)
        return {"status": "blocked", "error": error, "output": output, "coding_job_id": job_id}
    if "validate_coding_backend_pr_url" in (contract.get("semantic_validators") or []):
        output = {**output, **stage_contracts.trusted_coding_backend_fields(tool_audit)}
    if stage_contracts.resolve_stage_outcome(contract, output) == "blocked":
        return {"status": "blocked", "error": "stage contract outcome: blocked", "output": output, "coding_job_id": job_id}
    return {"status": "succeeded", "output": output, "coding_job_id": job_id}
