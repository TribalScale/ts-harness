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
    """Repo context; with ``include_intake_sources`` the requester's shared files ride along."""
    from ts_harness.workflows.bug_context import gather_bug_context

    output = gather_bug_context(task, config, memory=memory)
    if config.get("include_intake_sources"):
        output = {**output, **_gather_intake_sources(task, config, memory=memory)}
    return output


def _gather_intake_sources(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.intake.gather import gather_intake_sources

    return gather_intake_sources(task, config, memory=memory)


def _draft_screen_specs(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    """One spec per Figma screen, for the requester to correct at intake (intake/screen_specs.py)."""
    from ts_harness.intake.screen_specs import build_describer, draft_screen_specs, find_inventory
    from ts_harness.workflows.hooks._helpers import load_workflow_payload

    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    gathered = [
        item for item in tasks
        if item.get("step_name") == "gather_sources" and item.get("status") == "succeeded" and isinstance(item.get("output"), dict)
    ]
    refs = list((gathered[-1]["output"].get("design_refs") if gathered else None) or [])
    # A design inventory among the sources groups the frames and gives each screen its intent.
    inventory = find_inventory(gathered[-1]["output"].get("sources") if gathered else None)
    try:
        from ts_harness.intake.blobs import intake_store

        store = intake_store()
    except Exception:  # noqa: BLE001 - specs are drafted from the frame text alone
        store = None
    agent_id = str(config.get("agent_id") or "pm-agent")
    scope = {k: v for k, v in {"tenant": workflow.get("tenant_id"), "workflow": task.get("workflow_id"), "agent": agent_id}.items() if v}
    return draft_screen_specs(
        refs, describe=build_describer(agent_id=agent_id, scope=scope) if refs else None, store=store, inventory=inventory
    )


def _prepare_ticket_queue(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.jira.prepare_ticket_queue import prepare_ticket_queue

    return prepare_ticket_queue(task, config, memory=memory)


def _prepare_ticket_delivery(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.jira.ticket_delivery import prepare_ticket_delivery

    return prepare_ticket_delivery(task, config, memory=memory)


def _assign_filed_tickets(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.jira.assign_filed import assign_filed_tickets

    return assign_filed_tickets(task, config, memory=memory)


def _close_ticket(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.jira.tracking import close_ticket

    return close_ticket(task, config, memory=memory)


def _fill_ticket_pool(task: dict[str, Any], config: dict[str, Any], *, memory: Any) -> dict[str, Any]:
    from ts_harness.jira.ticket_pool import fill

    return fill(task, config, memory=memory)


SYSTEM_STEPS: dict[str, Callable[..., dict[str, Any]]] = {
    **release.SYSTEM_STEPS,
    "fork_work_order_wave": work_order_waves.fork_wave,
    "fork_children": child_workflows.fork_children,
    "ensure_fresh": _ensure_fresh,
    "review_precheck": _review_precheck,
    "finalize_code_review": _finalize_code_review,
    "gather_bug_context": _gather_bug_context,
    "gather_intake_sources": _gather_intake_sources,
    "draft_screen_specs": _draft_screen_specs,
    "prepare_ticket_queue": _prepare_ticket_queue,
    "prepare_ticket_delivery": _prepare_ticket_delivery,
    "assign_filed_tickets": _assign_filed_tickets,
    "close_ticket": _close_ticket,
    "fill_ticket_pool": _fill_ticket_pool,
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
