"""Expand-stage handler for the workflow engine."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import workflow_definitions  # noqa: E402


class ExpandHandler:
    """Validates and proposes expansion plans for expand-kind stages.

    An expand stage runs an agent that returns ``proposed_tasks`` in its
    output.  Before those tasks are created in the workflow DB the runner
    validates them against the skeleton's policy constraints and then calls
    ``memory_client.propose_plan()``.
    """

    def __init__(self, memory_client: Any) -> None:
        self._memory = memory_client

    def handle(
        self,
        task: dict[str, Any],
        stage: workflow_definitions.Stage,
        skeleton: workflow_definitions.WorkflowSkeleton,
        proposed_tasks: list[dict[str, Any]],
        existing_step_names: set[str] | None = None,
    ) -> dict[str, Any]:
        """Validate *proposed_tasks* and register them as a pending plan.

        Returns the plan dict from memory-api (contains at least ``plan_id``).
        Raises :class:`ValueError` on validation failure.
        """
        validated = workflow_definitions.validate_proposed_tasks(
            skeleton,
            stage,
            proposed_tasks,
            existing_step_names or set(),
        )
        plan = self._memory.propose_plan(
            task["workflow_id"],
            task["id"],
            validated,
        )
        return plan

    def needs_approval(self, stage: workflow_definitions.Stage) -> bool:
        return bool(stage.plan_approval_required)
