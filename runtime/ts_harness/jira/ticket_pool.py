"""Deliver a project's Jira tickets in parallel: a rolling pool of ``implement_jira_ticket`` children.

``implement all <PROJECT>`` used to build one ticket at a time. With a parallel limit above 1
(project setting ``max_parallel_tickets``), the ``implement`` phase runs ``implement_jira_pool``
instead:

* ``fill_pool`` starts one child per ticket whose blockers are all done, up to the limit;
* ``join_pool`` waits on the children. Each time one finishes (merged and closed, or stopped)
  :func:`refill` starts the tickets that became ready, so a slot never waits for a whole wave;
* the pool finishes when nothing is running and nothing more can start. Tickets left behind
  a stopped or failed blocker are reported, not started.

Ready means: not started yet, not done in Jira, and every "is blocked by" issue done. When
several are ready, tickets for a repository with nothing running go first, so parallel PRs
rarely touch the same repository.
"""

from __future__ import annotations

import logging
from typing import Any

from ts_harness.jira.spec_hydrate import open_blockers
from ts_harness.workflows.hooks._helpers import load_workflow_payload

logger = logging.getLogger(__name__)

CHILD_WORKFLOW_TYPE = "implement_jira_ticket"
DEFAULT_LIMIT = 2
MAX_LIMIT = 8
_DONE = {"done", "closed", "resolved", "cancelled", "canceled", "won't do"}


def _task_input(task: dict[str, Any]) -> dict[str, Any]:
    return dict(task.get("input") or {}) if isinstance(task.get("input"), dict) else {}


def pool_limit(value: Any) -> int:
    try:
        return max(1, min(MAX_LIMIT, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _status_done(issue: dict[str, Any]) -> bool:
    status = ((issue.get("fields") or {}).get("status") or {}) if isinstance(issue, dict) else {}
    name = str(status.get("name") or "").strip().lower()
    category = str(((status.get("statusCategory") or {}).get("key")) or "").strip().lower()
    return name in _DONE or category == "done"


def _repo_of(issue: dict[str, Any], default: str = "") -> str:
    """The ``Repo:`` pointer line of the ticket description, else ``default``."""
    import re

    text = str(((issue.get("fields") or {}).get("description")) or "")
    if not isinstance(((issue.get("fields") or {}).get("description")), str):
        import json

        text = json.dumps((issue.get("fields") or {}).get("description") or "")
    match = re.search(r"Repo:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    return match.group(1).lower() if match else default


def ready_tickets(jira: Any, keys: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """(ready tickets as ``{"key", "repo"}`` in queue order, keys already done in Jira)."""
    ready, done = [], []
    for key in keys:
        try:
            issue = jira.get_issue(key) or {}
        except Exception as exc:  # noqa: BLE001 - try again on the next refill
            logger.warning("ticket pool: %s not read: %s", key, exc)
            continue
        if _status_done(issue):
            done.append(key)
            continue
        if open_blockers((issue.get("fields") or {}).get("issuelinks")):
            continue
        ready.append({"key": key, "repo": _repo_of(issue)})
    return ready, done


def _choose(ready: list[dict[str, Any]], running_repos: list[str], room: int) -> list[dict[str, Any]]:
    """Up to ``room`` tickets, those for an idle repository first, queue order otherwise."""
    if room <= 0:
        return []
    busy = set(running_repos)
    idle = [item for item in ready if item["repo"] and item["repo"] not in busy]
    chosen: list[dict[str, Any]] = []
    for item in idle:
        if len(chosen) < room and item["repo"] not in {c["repo"] for c in chosen}:
            chosen.append(item)
    for item in ready:
        if len(chosen) < room and item not in chosen:
            chosen.append(item)
    return chosen


def _child_input(shared: dict[str, Any], key: str) -> dict[str, Any]:
    from ts_harness.workflows.program_adapters import _build_implement_inputs

    return _build_implement_inputs(summary=str(shared.get("summary") or ""), artifacts={**shared, "jira_issue_key": key})


def _fork(memory: Any, *, parent: dict[str, Any], shared: dict[str, Any], items: list[dict[str, Any]], actor: str,
          version: Any = None) -> list[dict[str, str]]:
    from ts_harness.workflows.hooks.fork_subworkflow import fork_child

    # Children post to the same chat thread as the pool (PR ready, blocked, decisions).
    try:
        record, _ = load_workflow_payload(memory, str(parent.get("workflow_id") or ""))
    except Exception:  # noqa: BLE001 - children then run without a thread
        record = {}
    children = []
    for item in items:
        child_id = fork_child(
            memory,
            parent_workflow_id=str(parent.get("workflow_id") or ""),
            tenant_id=str(parent.get("tenant_id") or "default"),
            child_type=CHILD_WORKFLOW_TYPE,
            intake_input=_child_input(shared, item["key"]),
            actor=actor,
            definition_version=version,
            source_channel=str(record.get("source_channel") or "") or None,
            requester_actor_id=str(record.get("requester_actor_id") or "") or None,
        )
        children.append({"workflow_id": child_id, "work_order_id": item["key"], "label": item["key"], "repo": item["repo"]})
    return children


def _shared(task_input: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in task_input.items() if not key.startswith("_") and key != "jira_issue_key"}


def fill(task: dict[str, Any], config: dict[str, Any], *, memory: Any, jira: Any = None,
         actor: str = "workflow-runner") -> dict[str, Any]:
    """System step ``fill_ticket_pool``: start the first ready tickets."""
    workflow_id = str(task.get("workflow_id") or "")
    _, tasks = load_workflow_payload(memory, workflow_id)
    stored = next((item for item in tasks if str(item.get("id")) == str(task.get("id"))), {})
    task_input = {**_task_input(stored), **_task_input(task)}
    queue = [str(key) for key in task_input.get("jira_issue_keys") or [] if key]
    if not queue and task_input.get("jira_issue_key"):
        queue = [str(task_input["jira_issue_key"])]
    if not queue:
        raise ValueError("the ticket pool has no tickets to deliver")
    limit = pool_limit(task_input.get("max_parallel_tickets"))
    if jira is None:
        from ts_harness.integrations.jira_client import JiraClient

        jira = JiraClient.from_env()
    ready, done = ready_tickets(jira, queue)
    chosen = _choose(ready, [], limit)
    if not chosen:
        raise ValueError("no ticket in the queue is ready: every remaining ticket waits on an open blocker")
    children = _fork(memory, parent={"workflow_id": workflow_id, "tenant_id": task.get("tenant_id")},
                     shared=_shared(task_input), items=chosen, actor=actor, version=task_input.get("_child_version"))
    return {
        "children": children,
        "queue": queue,
        "max_parallel_tickets": limit,
        "already_done": done,
        "summary": f"Started {', '.join(c['label'] for c in children)} in parallel (up to {limit} at a time).",
    }


def refill(ctx: Any, task: dict[str, Any], *, jira: Any = None) -> list[dict[str, str]]:
    """Start the tickets that became ready since the last child finished; return every pinned child."""
    from ts_harness.workflows.work_order_waves import child_state, pinned_children

    memory = ctx.memory_client
    children = pinned_children(task)
    _, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    fill_task = next((item for item in tasks if item.get("step_name") == "fill_pool"), {})
    fill_output = fill_task.get("output") if isinstance(fill_task.get("output"), dict) else {}
    queue = [str(key) for key in fill_output.get("queue") or []]
    limit = pool_limit(fill_output.get("max_parallel_tickets"))
    running, finished_bad = [], []
    for child in children:
        workflow, child_tasks = load_workflow_payload(memory, child["workflow_id"])
        state = child_state(workflow, child_tasks)
        if state is None:
            running.append(child)
        elif state != "succeeded":
            finished_bad.append(child["label"])
    started = {child.get("label") for child in children}
    remaining = [key for key in queue if key not in started]
    room = limit - len(running)
    if not remaining or room <= 0:
        return children
    if jira is None:
        from ts_harness.integrations.jira_client import JiraClient

        jira = JiraClient.from_env()
    ready, _done = ready_tickets(jira, remaining)
    chosen = _choose(ready, [str(child.get("repo") or "") for child in running], room)
    if not chosen:
        return children
    fill_input = _task_input(fill_task)
    new = _fork(memory, parent={"workflow_id": task.get("workflow_id"), "tenant_id": task.get("tenant_id")},
                shared=_shared(fill_input), items=chosen, actor=ctx.actor, version=fill_input.get("_child_version"))
    pinned = children + new
    options = {**(_task_input(task).get("_wait_options") or {}), "children": pinned}
    memory.patch_task_input(str(task["id"]), {**_task_input(task), "_wait_options": options}, actor=ctx.actor)
    task.setdefault("input", {})["_wait_options"] = options
    logger.info("ticket pool %s started %s", task.get("workflow_id"), [c["label"] for c in new])
    return pinned


def left_behind(queue: list[str], children: list[dict[str, str]]) -> list[str]:
    started = {child.get("label") for child in children}
    return [key for key in queue if key not in started]
