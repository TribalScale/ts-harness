"""Write the review stage's proposals as notes that still need a person."""

from __future__ import annotations

from typing import Any

from ts_harness.learning.proposals import apply_proposals
from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext) -> dict[str, Any]:
    output = (ctx.dispatch_result or {}).get("output") or {}
    if not isinstance(output, dict):
        output = {}
    proposals = output.get("proposals") if isinstance(output.get("proposals"), list) else []
    task_input = (ctx.task or {}).get("input") if isinstance((ctx.task or {}).get("input"), dict) else {}
    source_program_id = str(task_input.get("source_program_id") or output.get("source_program_id") or "")
    project_id = str(task_input.get("project_id") or "")
    existing = list(task_input.get("project_notes") or []) + list(task_input.get("org_notes") or [])
    return apply_proposals(
        ctx.memory_client,
        tenant_id=ctx.tenant_id or "default",
        project_id=project_id,
        source_program_id=source_program_id,
        proposals=proposals,
        existing_notes=[note for note in existing if isinstance(note, dict)],
    )
