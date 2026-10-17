"""Compensate-stage handler for the workflow engine.

A *compensate* stage is dispatched when downstream work fails and the
workflow definition requires rollback/undo logic.  The runtime semantics
mirror a regular ``task`` dispatch, but the payload is annotated with
``_compensate: true`` so that agents recognise the compensation context.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import workflow_definitions  # noqa: E402


class CompensateHandler:
    """Builds dispatch payloads for compensate-kind stages."""

    def build_dispatch_payload(
        self,
        task: dict[str, Any],
        stage: workflow_definitions.Stage,
        failed_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a dispatch payload annotated for compensation context."""
        payload = {
            "workflow_id": task.get("workflow_id"),
            "task_id": task.get("id"),
            "workflow_type": task.get("workflow_type", "generic"),
            "step_name": task.get("step_name"),
            "agent_id": task.get("agent_id"),
            "tenant_id": task.get("tenant_id", ""),
            "input": {
                **(task.get("input") or {}),
                "_compensate": True,
                "_failed_task": failed_task or {},
            },
            "required_artifacts": stage.required_artifacts,
            "skill_hints": stage.skill_hints,
        }
        if stage.tool_allow is not None:
            payload["tool_allow"] = list(stage.tool_allow)
        return payload

    @staticmethod
    def is_compensatable(stage: workflow_definitions.Stage) -> bool:
        return stage.kind == "compensate"
