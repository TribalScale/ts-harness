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


def ready_tickets(
    jira: Any, keys: list[str], waiting: dict[str, list[str]] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """(ready tickets as ``{"key", "repo"}`` in queue order, keys already done in Jira).

    ``waiting``, when given, collects each blocked ticket's open blockers.
    """
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
        blockers = open_blockers((issue.get("fields") or {}).get("issuelinks"))
        if blockers:
            if waiting is not None:
                waiting[key] = list(blockers)
            continue
        ready.append({"key": key, "repo": _repo_of(issue)})
    return ready, done


def _rows(result: Any, key: str) -> list[dict[str, Any]]:
    rows = result.get(key) if isinstance(result, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def busy_elsewhere(memory: Any, tenant_id: str, own: set[str]) -> dict[str, str]:
    """``{ticket key: workflow id}`` for tickets a run other than ``own`` is building now."""
    try:
        rows = _rows(memory.running_ticket_work(tenant_id=tenant_id or "default"), "tickets")
    except Exception as exc:  # noqa: BLE001 - without the check the pool behaves as before
        logger.warning("ticket pool: running deliveries not read: %s", exc)
        return {}
    busy: dict[str, str] = {}
    for row in rows:
        key, workflow_id = str(row.get("key") or ""), str(row.get("workflow_id") or "")
        if key and workflow_id and workflow_id not in own and str(row.get("parent_workflow_id") or "") not in own:
            busy.setdefault(key, workflow_id)
    return busy


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
    waiting: dict[str, list[str]] = {}
    ready, done = ready_tickets(jira, queue, waiting)
    # A ticket another run is already building is left to that run (never built twice).
    busy = busy_elsewhere(memory, str(task.get("tenant_id") or "default"), {workflow_id})
    ready = [item for item in ready if item["key"] not in busy]
    chosen = _choose(ready, [], limit)
    queued_busy = sorted(key for key in busy if key in set(queue))
    if not chosen and len(done) == len(queue):
        raise ValueError("every ticket in the queue is already done in Jira: nothing to deliver")
    if not chosen and queued_busy:
        raise ValueError(
            "no ticket can start yet: " + ", ".join(queued_busy) + " is already being built by another run, "
            "and the rest wait on blockers"
        )
    if not chosen:
        raise ValueError("no ticket in the queue is ready: every remaining ticket waits on an open blocker")
    children = _fork(memory, parent={"workflow_id": workflow_id, "tenant_id": task.get("tenant_id")},
                     shared=_shared(task_input), items=chosen, actor=actor, version=task_input.get("_child_version"))
    return {
        "children": children,
        "queue": queue,
        "max_parallel_tickets": limit,
        "already_done": done,
        "busy_elsewhere": sorted(busy),
        "summary": start_text([c["label"] for c in children], waiting, limit)
        + (f" Left to the run already building it: {', '.join(sorted(busy))}." if busy else ""),
    }


def start_text(started: list[str], waiting: dict[str, list[str]], limit: int) -> str:
    """What the pool started and why the rest wait: no "in parallel" when only one could start."""
    from ts_harness.jira.ticket_queue import issue_sort_key

    text = f"Started {_and(started)}." if len(started) < 2 else f"Started {_and(started)} in parallel."
    if limit > 1:
        text += f" Up to {limit} tickets run at once."
    if not waiting:
        return text
    on_started = sorted((k for k, b in waiting.items() if set(b) <= set(started)), key=issue_sort_key)
    rest = sorted((k for k in waiting if k not in on_started), key=issue_sort_key)
    parts = []
    if on_started:
        parts.append(f"{_and(on_started)} {'waits' if len(on_started) == 1 else 'wait'} on {_and(started)}")
    if len(rest) == 1:
        parts.append(f"{rest[0]} waits on {_and(sorted(waiting[rest[0]], key=issue_sort_key))}")
    elif rest:
        parts.append(f"{len(rest)} more wait on later tickets" if on_started else f"the other {len(rest)} wait on their blockers")
    sentence = "; ".join(parts)
    return f"{text} {sentence[0].upper()}{sentence[1:]}."


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


MAX_WAITING_FOR_MERGE = 4


def max_waiting_for_merge() -> int:
    """How many of a pool's PRs may wait for a merge at once (``TS_HARNESS_POOL_MAX_WAITING_MERGE``, default 4)."""
    import os

    try:
        return max(1, int(os.environ.get("TS_HARNESS_POOL_MAX_WAITING_MERGE") or MAX_WAITING_FOR_MERGE))
    except ValueError:
        return MAX_WAITING_FOR_MERGE


def waiting_for_merge(tasks: list[dict[str, Any]]) -> bool:
    """The ticket's PR passed its checks, review and QA and waits for a person to merge it."""
    statuses = [str(t.get("status") or "") for t in tasks]
    if any(status in {"running", "claimed"} for status in statuses):
        return False
    return any(
        t.get("step_name") == "await_human_merge" and t.get("status") == "blocked"
        and str(t.get("error") or "").startswith("awaiting_external:github.merge")
        for t in tasks
    )


def merge_wait_started(memory: Any, workflow_id: str, *, actor: str = "workflow-runner") -> bool:
    """A pool child started waiting for its merge: let the pool use the slot it frees."""
    from ts_harness.workflows.work_order_waves import CHILD_EVENT

    try:
        workflow, _tasks = load_workflow_payload(memory, workflow_id)
        parent_id = str(workflow.get("parent_workflow_id") or "")
        if not parent_id:
            return False
        parent, _ = load_workflow_payload(memory, parent_id)
        if str(parent.get("workflow_type") or "") != "implement_jira_pool":
            return False
        from ts_harness.workflows.engine import get_workflow_engine

        get_workflow_engine().resume_external(
            parent_id, event_type=CHILD_EVENT, payload={"ready_to_merge": workflow_id}, actor=actor,
            tenant_id=str(workflow.get("tenant_id") or "default"),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - the pool re-checks on its next child event
        logger.info("ticket pool not told %s waits for its merge: %s", workflow_id, exc)
        return False


def current_limit(memory: Any, task: dict[str, Any], queue: list[str], started_with: int) -> int:
    """The project's parallel limit as it is now, so raising it reaches a running pool; else the limit it started with."""
    key = next((str(k).split("-", 1)[0] for k in queue if "-" in str(k)), "")
    if not key:
        return started_with
    from ts_harness.jira.prepare_ticket_queue import project_ticket_limit

    live = project_ticket_limit(memory, str(task.get("tenant_id") or "default"), key)
    if live is not None and live != started_with:
        logger.info("ticket pool %s parallel limit %s -> %s (project setting)", task.get("workflow_id"), started_with, live)
    return live if live is not None else started_with


def refill(ctx: Any, task: dict[str, Any], *, jira: Any = None) -> list[dict[str, str]]:
    """Start the tickets that became ready since the last child finished; return every pinned child."""
    from ts_harness.workflows.work_order_waves import child_state, pinned_children

    memory = ctx.memory_client
    children = pinned_children(task)
    _, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    fill_task = next((item for item in tasks if item.get("step_name") == "fill_pool"), {})
    fill_output = fill_task.get("output") if isinstance(fill_task.get("output"), dict) else {}
    queue = [str(key) for key in fill_output.get("queue") or []]
    limit = current_limit(memory, task, queue, pool_limit(fill_output.get("max_parallel_tickets")))
    running, finished_bad, waiting_merge = [], [], []
    for child in children:
        workflow, child_tasks = load_workflow_payload(memory, child["workflow_id"])
        state = child_state(workflow, child_tasks)
        if state is None:
            running.append(child)
            if waiting_for_merge(child_tasks):
                waiting_merge.append(child)
        elif state != "succeeded":
            finished_bad.append(child["label"])
    started = {child.get("label") for child in children}
    remaining = [key for key in queue if key not in started]
    # A ticket waiting for a person to merge its PR no longer holds a slot, so independent
    # tickets keep going; at most ``max_waiting_for_merge()`` such PRs wait at once.
    active = len(running) - len(waiting_merge)
    room = limit - active if len(waiting_merge) < max_waiting_for_merge() else 0
    if not remaining or room <= 0:
        return children
    if jira is None:
        from ts_harness.integrations.jira_client import JiraClient

        jira = JiraClient.from_env()
    ready, _done = ready_tickets(jira, remaining)
    own = {str(task.get("workflow_id") or ""), *(str(child.get("workflow_id") or "") for child in children)}
    busy = busy_elsewhere(memory, str(task.get("tenant_id") or "default"), own)
    ready = [item for item in ready if item["key"] not in busy]
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


def waiting_on_other_runs(ctx: Any, task: dict[str, Any], children: list[dict[str, str]], *, jira: Any = None) -> list[str]:
    """Remaining tickets the pool must keep waiting for: ones another run is building, or ones
    blocked by such a ticket. Their run finishing wakes the pool (``wake_pools``)."""
    memory = ctx.memory_client
    _, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    fill_task = next((item for item in tasks if item.get("step_name") == "fill_pool"), {})
    queue = [str(key) for key in ((fill_task.get("output") or {}).get("queue") or [])]
    started = {child.get("label") for child in children}
    remaining = [key for key in queue if key not in started]
    if not remaining:
        return []
    own = {str(task.get("workflow_id") or ""), *(str(child.get("workflow_id") or "") for child in children)}
    busy = busy_elsewhere(memory, str(task.get("tenant_id") or "default"), own)
    if not busy:
        return []
    if jira is None:
        from ts_harness.integrations.jira_client import JiraClient

        jira = JiraClient.from_env()
    waiting: dict[str, list[str]] = {}
    _ready, done = ready_tickets(jira, remaining, waiting)
    return [key for key in remaining if key not in done and (key in busy or set(waiting.get(key) or []) & set(busy))]


def wake_pools(memory: Any, key: str, *, tenant_id: str = "default", actor: str = "workflow-runner",
               exclude: str = "") -> list[str]:
    """A ticket another run built has finished: let every pool waiting on it look again."""
    from ts_harness.workflows.work_order_waves import CHILD_EVENT

    try:
        pools = _rows(memory.running_ticket_work(tenant_id=tenant_id or "default"), "pools")
    except Exception:  # noqa: BLE001
        return []
    woken: list[str] = []
    for pool in pools:
        if str(pool.get("workflow_id") or "") == exclude:
            continue  # the pool's own child: it already tells its parent
        if key not in [str(item) for item in pool.get("queue") or []] or pool.get("join_status") != "blocked":
            continue
        try:
            from ts_harness.workflows.engine import get_workflow_engine

            get_workflow_engine().resume_external(
                str(pool["workflow_id"]), event_type=CHILD_EVENT,
                payload={"ticket_finished_elsewhere": key}, actor=actor, tenant_id=tenant_id,
            )
            woken.append(str(pool["workflow_id"]))
        except Exception as exc:  # noqa: BLE001 - the pool re-checks on its own next child event
            logger.info("ticket pool %s not woken for %s: %s", pool.get("workflow_id"), key, exc)
    return woken


GAVE_UP_PREFIX = "external_blocked:workflow.child_terminal"


def reopen_pools(memory: Any, *, tenant_id: str = "default", actor: str = "workflow-runner") -> list[str]:
    """A pool that stopped because a ticket failed or was stopped: when that ticket recovered
    (a person retried or revised it, and it is running or merged now), wait on the children again
    and start the tickets it left behind."""
    from ts_harness.workflows.work_order_waves import CHILD_EVENT, child_state, pinned_children

    try:
        pools = _rows(memory.running_ticket_work(tenant_id=tenant_id or "default"), "pools")
    except Exception:  # noqa: BLE001
        return []
    reopened: list[str] = []
    for pool in pools:
        if pool.get("join_status") != "blocked":
            continue
        workflow_id = str(pool.get("workflow_id") or "")
        try:
            _, tasks = load_workflow_payload(memory, workflow_id)
            join = next((t for t in tasks if str(t.get("id")) == str(pool.get("join_task_id"))), None)
            if join is not None and _stale_child_wait(memory, join):
                # It waits for a child to finish, but every child already has: the event that
                # would wake it was lost (a restart between the last child ending and the join).
                from ts_harness.workflows.engine import get_workflow_engine

                get_workflow_engine().resume_external(
                    workflow_id, event_type=CHILD_EVENT, payload={"reopened": "every child had already finished"},
                    actor=actor, tenant_id=tenant_id,
                )
                reopened.append(workflow_id)
                logger.info("ticket pool %s woken: every child had already finished", workflow_id)
                continue
            if join is None or not str(join.get("error") or "").startswith(GAVE_UP_PREFIX):
                continue
            states = []
            for child in pinned_children(join):
                child_workflow, child_tasks = load_workflow_payload(memory, child["workflow_id"])
                states.append(child_state(child_workflow, child_tasks))
            if any(state in {"failed", "cancelled"} for state in states):
                continue  # still a failed ticket: the person's decision is pending
            if not _worth_reopening(str(join.get("error") or ""), pool, join):
                continue  # nothing changed since it gave up: reopening would only block again
            memory.block_task_api(str(join["id"]), f"awaiting_external:{CHILD_EVENT}", actor=actor)
            from ts_harness.workflows.engine import get_workflow_engine

            get_workflow_engine().resume_external(
                workflow_id, event_type=CHILD_EVENT, payload={"reopened": "the failed ticket recovered"},
                actor=actor, tenant_id=tenant_id,
            )
            reopened.append(workflow_id)
            logger.info("ticket pool %s reopened: its failed ticket recovered", workflow_id)
        except Exception as exc:  # noqa: BLE001 - checked again on the next poll
            logger.info("ticket pool %s not reopened: %s", workflow_id, exc)
    return reopened


STALE_WAIT_SECONDS = 300


def _stale_child_wait(memory: Any, join: dict[str, Any], *, now: float | None = None) -> bool:
    """The join has waited for a child event for a while, and no child is still running."""
    from datetime import datetime, timezone

    from ts_harness.workflows.work_order_waves import CHILD_EVENT, child_state, pinned_children

    if str(join.get("error") or "") != f"awaiting_external:{CHILD_EVENT}":
        return False
    try:
        since = datetime.fromisoformat(str(join.get("updated_at") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    current = now if now is not None else datetime.now(timezone.utc).timestamp()
    if current - since < STALE_WAIT_SECONDS:
        return False
    children = pinned_children(join)
    if not children:
        return False
    for child in children:
        child_workflow, child_tasks = load_workflow_payload(memory, child["workflow_id"])
        if child_state(child_workflow, child_tasks) is None:
            return False
    return True


_NOT_STARTED_MARK = "tickets not started because a blocker was not delivered: "
_FAILED_MARK = "stopped or failed: "


def _worth_reopening(error: str, pool: dict[str, Any], join: dict[str, Any], *, jira: Any = None) -> bool:
    """Reopen when a failed ticket recovered, or when a ticket it left behind is no longer open
    (another run delivered it, or its blocker is done). A pool that gave up only because tickets
    were not started, with the same tickets still open, would block again on every poll."""
    if _FAILED_MARK in error or " failed: " in error:
        return True
    if _NOT_STARTED_MARK not in error:
        return True
    recorded = error.split(_NOT_STARTED_MARK, 1)[1].split(";", 1)[0]
    before = {key.strip() for key in recorded.split(",") if key.strip()}
    from ts_harness.workflows.work_order_waves import pinned_children

    queue = [str(key) for key in pool.get("queue") or []]
    now = set(open_left_behind(queue, pinned_children(join), jira=jira)) if queue else before
    if now != before:
        return True
    if not now:
        return False
    # Still open: worth another look only when one of them is ready to start now.
    if jira is None:
        try:
            from ts_harness.integrations.jira_client import JiraClient

            jira = JiraClient.from_env()
        except Exception:  # noqa: BLE001
            return False
    ready, _done = ready_tickets(jira, sorted(now))
    return bool(ready)


def left_behind(queue: list[str], children: list[dict[str, str]], *, jira: Any = None) -> list[str]:
    """Queued tickets the pool never started that are still open in Jira.

    A ticket another run delivered (Done in Jira) is not left behind: the pool skipped it on
    purpose. Without a Jira client every unstarted ticket counts.
    """
    started = {child.get("label") for child in children}
    remaining = [key for key in queue if key not in started]
    if not remaining or jira is None:
        return remaining
    _ready, done = ready_tickets(jira, remaining)
    return [key for key in remaining if key not in done]


def open_left_behind(queue: list[str], children: list[dict[str, str]], *, jira: Any = None) -> list[str]:
    """``left_behind`` with the configured Jira client; unstarted tickets all count if Jira cannot be reached."""
    started = {child.get("label") for child in children}
    if all(key in started for key in queue):
        return []
    if jira is None:
        try:
            from ts_harness.integrations.jira_client import JiraClient

            jira = JiraClient.from_env()
        except Exception as exc:  # noqa: BLE001 - no Jira: report every unstarted ticket
            logger.warning("ticket pool: Jira not available to check left-behind tickets: %s", exc)
            jira = None
    return left_behind(queue, children, jira=jira)
