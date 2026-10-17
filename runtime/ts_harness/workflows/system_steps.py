"""Deterministic system steps the runner executes for ``barrier`` stages (no agent)."""

from __future__ import annotations

from typing import Any, Callable

from ts_harness.integrations.github_client import GitHubError
from ts_harness.workflows import child_workflows, release, work_order_waves

class StepBlocked(ValueError):
    """A system step outcome that blocks with an output (e.g. for an ``on_block`` loop)."""

    def __init__(self, reason: str, *, output: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.output = dict(output or {})


def _ensure_fresh(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.workflows.branch_freshness import ensure_fresh

    return ensure_fresh(task, config, memory=memory)


def _review_precheck(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.workflows.code_review import review_precheck

    return review_precheck(task, config, memory=memory)


def _finalize_code_review(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.workflows.code_review import finalize_code_review

    return finalize_code_review(task, config, memory=memory)


def _gather_bug_context(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.workflows.bug_context import gather_bug_context

    return gather_bug_context(task, config, memory=memory)


SYSTEM_STEPS: dict[str, Callable[..., dict[str, Any]]] = {
    **release.SYSTEM_STEPS,
    "fork_work_order_wave": work_order_waves.fork_wave,
    "fork_children": child_workflows.fork_children,
    "ensure_fresh": _ensure_fresh,
    "review_precheck": _review_precheck,
    "finalize_code_review": _finalize_code_review,
    "gather_bug_context": _gather_bug_context,
}


def run_system_step(hook: str, task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    """``succeeded`` with output, or ``blocked`` with a reason (and output for ``StepBlocked``)."""
    try:
        output = SYSTEM_STEPS[hook](task, config, memory=memory)
    except StepBlocked as exc:
        return {"status": "blocked", "reason": str(exc), "output": exc.output}
    except (release.ReleaseBlocked, GitHubError, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}
    return {"status": "succeeded", "output": output}
