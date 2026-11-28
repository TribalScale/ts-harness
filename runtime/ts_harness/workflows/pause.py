"""Pause and resume coding work: one ticket, a program or ticket queue, or everything.

A pause never stops a step that is already running: a coding run, a code review or a QA run
in progress finishes. What it holds is the *next* coding run.

Levels
------
* **Ticket** (a delivery workflow) and **ticket queue** (a pool workflow, which covers every
  ticket it started): the runner creates no coding job for a paused workflow or any workflow
  below it, whether a first run, a revision, a refresh or an automatic retry. The task is held
  instead: blocked with ``awaiting_external:workflow.resumed`` and a ``_pause_hold`` marker in
  its input (who paused, when, and what kind of run waits). That wait is not a decision, so the
  Inbox's stuck list, the stuck-step arbiter, the readiness re-check, the ops alert for stuck
  work and the wave joins all leave it alone. A coding job that was already queued stays queued
  (never cancelled); the memory API's claim skips it until the pause ends
  (``scripts/memory_routes/pause.py``).
* **Program**: the same for every workflow of the program and its sub-programs, and a ticket
  pool of the program starts no new tickets (``fill_pool`` is held, ``refill`` starts nothing).
* **Everything** (admins): the coding launcher claims and starts no queued job. Jobs keep
  queueing; running ones finish; code review and QA keep running.

Storage
-------
One JSON document in ``runtime_kv`` (namespace ``coding-pause``, key ``state``), shared by every
replica and kept in Postgres, so a pause survives restarts and redeploys::

    {"global": {"by", "at", "reason"} | null,
     "programs": {"<program id>": {"by", "at", "reason", "label"}},
     "workflows": {"<workflow id>": {"by", "at", "reason", "label", "kind", "program_id"}}}

Updates are compare-and-set (``storage/kv.py``), so two people pausing at once never lose one.
The memory API reads the same document inside its claim and release transactions.

Resume
------
Resuming removes the entry, then releases every held task no longer covered by any pause back
to ``pending`` with its input intact (a held revision keeps its review or QA feedback), moves
the scope's wait deadlines on by the time spent paused, and wakes the scope's ticket pools so
they start the tickets that became ready. A ticket resumed while its program is still paused
stays held, and the reply says so.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

NAMESPACE = "coding-pause"
KEY = "state"
EVENT_TYPE = "workflow.resumed"
WAIT_ERROR = f"awaiting_external:{EVENT_TYPE}"
HOLD_KEY = "_pause_hold"
LAST_KEY = "_pause_last"
SCOPES = ("ticket", "program", "global")
SCOPE_ALIASES = {"ticket": "ticket", "workflow": "ticket", "queue": "ticket", "program": "program",
                 "global": "global", "all": "global", "everything": "global"}
QUEUE_TYPES = frozenset({"implement_jira_pool"})
# Input keys a revise task carries (hooks/revise_on_reject.py); a held task with any of them is a
# revision waiting, shown as "Paused (revision waiting)".
_REVISION_KEYS = ("_revision_source", "_revision_grant", "_revision_budget", "review_feedback",
                  "qa_feedback", "_guided", "_auto_grant", "revision_number")
KIND_LABELS = {
    "revision": "revision waiting",
    "first_run": "first coding run waiting",
    "refresh": "branch refresh waiting",
    "retry": "retry waiting",
    "queue_start": "new tickets waiting",
}


class PauseError(ValueError):
    """The pause or resume request is not valid (unknown scope, missing target)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def empty_state() -> dict[str, Any]:
    return {"global": None, "programs": {}, "workflows": {}}


def normalize(value: Any) -> dict[str, Any]:
    state = empty_state()
    if not isinstance(value, dict):
        return state
    if isinstance(value.get("global"), dict):
        state["global"] = dict(value["global"])
    for name in ("programs", "workflows"):
        entries = value.get(name)
        if isinstance(entries, dict):
            state[name] = {str(k): dict(v) for k, v in entries.items() if k and isinstance(v, dict)}
    return state


def is_empty(state: dict[str, Any] | None) -> bool:
    state = state or {}
    return not state.get("global") and not state.get("programs") and not state.get("workflows")


def _client(client: Any) -> Any:
    if client is not None:
        return client
    from ts_harness.memory.client import get_memory_client

    return get_memory_client()


def read_state(client: Any = None, *, strict: bool = False) -> dict[str, Any]:
    """The pause document. A read failure means "nothing paused" unless ``strict``: holding work
    because the memory API blinked would be worse than running it."""
    try:
        return normalize((_client(client).kv_get(NAMESPACE, KEY) or {}).get("value"))
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        logger.warning("pause state unreadable (%s); treating nothing as paused", str(exc)[:200])
        return empty_state()


def global_entry(state: dict[str, Any] | None) -> dict[str, Any] | None:
    entry = (state or {}).get("global")
    return {**entry, "scope": "global", "target_id": ""} if isinstance(entry, dict) else None


def entry_for(
    state: dict[str, Any] | None,
    *,
    workflow_chain: list[str] | tuple[str, ...] = (),
    program_chain: list[str] | tuple[str, ...] = (),
    include_global: bool = False,
) -> dict[str, Any] | None:
    """The pause that covers a workflow, closest first: its own, a parent's (a queue), its
    program's or a parent program's, then (with ``include_global``) everything."""
    state = state or {}
    workflows = state.get("workflows") or {}
    for workflow_id in workflow_chain:
        entry = workflows.get(str(workflow_id))
        if isinstance(entry, dict):
            scope = "queue" if entry.get("kind") == "queue" else "ticket"
            return {**entry, "scope": scope, "target_id": str(workflow_id)}
    programs = state.get("programs") or {}
    for program_id in program_chain:
        entry = programs.get(str(program_id))
        if isinstance(entry, dict):
            return {**entry, "scope": "program", "target_id": str(program_id)}
    return global_entry(state) if include_global else None


def chains_for_task(memory: Any, task_id: str, *, workflow_id: str = "") -> tuple[list[str], list[str]]:
    """``(workflow chain, program chain)``: the task's workflow and its parents, and the program it
    belongs to and that program's parents (``GET /workflow/tasks/{id}/program``)."""
    try:
        found = memory.task_program(str(task_id)) or {}
    except Exception:  # noqa: BLE001 - an unreadable chain still checks the workflow itself
        found = {}
    workflows = [str(w) for w in found.get("workflow_chain") or [] if w]
    if not workflows:
        own = str(found.get("workflow_id") or workflow_id or "")
        workflows = [own] if own else []
    programs = [str(p) for p in found.get("program_chain") or [] if p]
    if not programs and found.get("program_id"):
        programs = [str(found["program_id"])]
    return workflows, programs


def task_hold(memory: Any, task: dict[str, Any], *, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The ticket, queue or program pause covering this task, or None. One kv read when nothing is
    paused; the chain lookup only when something is."""
    state = read_state(memory) if state is None else state
    if not state.get("workflows") and not state.get("programs"):
        return None
    workflows, programs = chains_for_task(memory, str(task.get("id") or ""), workflow_id=str(task.get("workflow_id") or ""))
    return entry_for(state, workflow_chain=workflows, program_chain=programs)


def without_paused(memory: Any, rows: list[dict[str, Any]], *, task_key: str = "id") -> list[dict[str, Any]]:
    """``rows`` (tasks with ``id`` and ``workflow_id``) minus those a ticket, queue or program pause
    covers. The arbiter, the readiness re-check, the stuck-work alert and the wait escalation use
    it so a paused ticket is never acted on, nagged about or counted as stuck."""
    if not rows:
        return rows
    state = read_state(memory)
    if not state.get("workflows") and not state.get("programs"):
        return rows
    kept = []
    for row in rows:
        task = {"id": row.get(task_key), "workflow_id": row.get("workflow_id")}
        if task_hold(memory, task, state=state) is None:
            kept.append(row)
    return kept


def hold_kind(task_input: dict[str, Any] | None, *, retry: bool = False) -> str:
    data = task_input if isinstance(task_input, dict) else {}
    if retry:
        return "retry"
    if data.get("refresh") or (isinstance(data.get("_bound"), dict) and data["_bound"].get("refresh")):
        return "refresh"
    if any(data.get(key) for key in _REVISION_KEYS):
        return "revision"
    return "first_run"


def hold_marker(entry: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "since": now_iso(),
        "kind": kind,
        "scope": str(entry.get("scope") or ""),
        "target_id": str(entry.get("target_id") or ""),
        "by": str(entry.get("by") or ""),
        "paused_at": str(entry.get("at") or ""),
        "reason": str(entry.get("reason") or "")[:500],
    }


def hold_task(memory: Any, task: dict[str, Any], entry: dict[str, Any], kind: str, *, actor: str = "workflow-runner") -> dict[str, Any]:
    """Park the task until resume: blocked on ``workflow.resumed`` with the hold marker (one atomic
    memory API call, so the task is never half-held)."""
    marker = hold_marker(entry, kind)
    memory.hold_paused_task(str(task["id"]), hold=marker, actor=actor)
    return marker


def status_label(kind: str) -> str:
    """``Paused (revision waiting)`` and the like, for Live, the ticket card and the program board."""
    detail = KIND_LABELS.get(str(kind or ""))
    return f"Paused ({detail})" if detail else "Paused"


def describe(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    who = str(entry.get("by") or "someone")
    when = str(entry.get("at") or "")[:16].replace("T", " ")
    scope = {"ticket": "this ticket", "queue": "its ticket queue", "program": "its program",
             "global": "all coding (admin)"}.get(str(entry.get("scope") or ""), "it")
    return f"Paused ({scope}) by {who}" + (f" at {when} UTC" if when else "")


def paused_seconds(entry: dict[str, Any] | None, *, now: datetime | None = None) -> int:
    started = _parse((entry or {}).get("at"))
    if started is None:
        return 0
    return max(0, int(((now or datetime.now(timezone.utc)) - started).total_seconds()))


# --- changing the state -------------------------------------------------------------------------


def scope_name(scope: str) -> str:
    name = SCOPE_ALIASES.get(str(scope or "").strip().lower())
    if not name:
        raise PauseError(f"unknown pause scope {scope!r}: use ticket, program or global")
    return name


def _slot(state: dict[str, Any], scope: str) -> dict[str, Any] | None:
    return {"ticket": state["workflows"], "program": state["programs"]}.get(scope)


def pause(
    scope: str,
    target_id: str = "",
    *,
    actor: str,
    reason: str = "",
    label: str = "",
    kind: str = "",
    program_id: str = "",
    client: Any = None,
) -> dict[str, Any]:
    """Record a pause. Pausing something already paused is a no-op (``already_paused``)."""
    from ts_harness.storage.kv import kv_update

    scope = scope_name(scope)
    target_id = str(target_id or "").strip()
    if scope != "global" and not target_id:
        raise PauseError(f"a {scope} pause needs the {scope} id")
    entry = {"by": str(actor or "someone"), "at": now_iso(), "reason": str(reason or "").strip()[:500]}
    if label:
        entry["label"] = str(label)[:200]
    if kind:
        entry["kind"] = str(kind)
    if program_id:
        entry["program_id"] = str(program_id)
    outcome: dict[str, Any] = {}

    def change(value: Any) -> dict[str, Any]:
        state = normalize(value)
        current = state["global"] if scope == "global" else (_slot(state, scope) or {}).get(target_id)
        if current:
            outcome.update(status="already_paused", entry=current)
            return state
        if scope == "global":
            state["global"] = entry
        else:
            _slot(state, scope)[target_id] = entry
        outcome.update(status="paused", entry=entry)
        return state

    kv_update(NAMESPACE, KEY, change, default=empty_state(), client=_client(client))
    return {"scope": scope, "target_id": target_id, **outcome}


def resume(scope: str, target_id: str = "", *, actor: str, client: Any = None) -> dict[str, Any]:
    """Remove a pause. Resuming something not paused is a no-op (``not_paused``)."""
    from ts_harness.storage.kv import kv_update

    scope = scope_name(scope)
    target_id = str(target_id or "").strip()
    if scope != "global" and not target_id:
        raise PauseError(f"a {scope} resume needs the {scope} id")
    outcome: dict[str, Any] = {}

    def change(value: Any) -> dict[str, Any]:
        state = normalize(value)
        if scope == "global":
            previous = state["global"]
            state["global"] = None
        else:
            previous = (_slot(state, scope) or {}).pop(target_id, None)
        outcome.update(status="resumed" if previous else "not_paused", entry=previous)
        return state

    kv_update(NAMESPACE, KEY, change, default=empty_state(), client=_client(client))
    result = {"scope": scope, "target_id": target_id, "resumed_by": str(actor or ""), **outcome}
    if outcome.get("entry"):
        result["paused_seconds"] = paused_seconds(outcome["entry"])
    return result


def release_after_resume(
    memory: Any,
    result: dict[str, Any],
    *,
    tenant_id: str = "default",
    actor: str,
) -> dict[str, Any]:
    """After :func:`resume`: release held tasks, move the scope's wait deadlines on by the time it
    was paused, and wake its ticket pools. Each part is best effort; the resume itself stands."""
    if result.get("status") != "resumed":
        return result
    out = dict(result)
    try:
        released = memory.release_paused_tasks(tenant_id=tenant_id, actor=actor) or {}
        out["released"] = list(released.get("released") or [])
        out["still_held"] = int(released.get("still_held") or 0)
    except Exception as exc:  # noqa: BLE001 - the next resume or the sweep releases them
        logger.warning("held tasks not released after resume: %s", exc)
        out["released"], out["release_error"] = [], str(exc)[:300]
    scope, target = str(result.get("scope") or ""), str(result.get("target_id") or "")
    seconds = int(result.get("paused_seconds") or 0)
    if scope in {"ticket", "program"} and target and seconds > 0:
        try:
            body = {"workflow_ids": [target]} if scope == "ticket" else {"program_ids": [target]}
            out["deadlines_moved"] = int((memory.shift_wait_deadlines(seconds=seconds, tenant_id=tenant_id, **body) or {}).get("shifted") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("wait deadlines not moved after resume: %s", exc)
    if scope in {"ticket", "program"}:
        try:
            from ts_harness.jira.ticket_pool import wake_resumed_pools

            out["pools_woken"] = wake_resumed_pools(memory, tenant_id=tenant_id, actor=actor, scope=scope, target_id=target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ticket pools not woken after resume: %s", exc)
    return out


def forget_finished(memory: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Drop ticket and queue pauses whose workflow has finished (stopped, cancelled, merged), so the
    Inbox and System page do not list them for ever. Returns the state as it is after that."""
    from ts_harness.storage.kv import kv_update

    finished: list[str] = []
    for workflow_id in list((state.get("workflows") or {}).keys()):
        try:
            status = str(((memory.get_workflow(workflow_id) or {}).get("workflow") or {}).get("status") or "")
        except Exception:  # noqa: BLE001 - unknown: keep it
            continue
        if status and status not in {"running", "blocked"}:
            finished.append(workflow_id)
    if not finished:
        return state

    def change(value: Any) -> dict[str, Any]:
        current = normalize(value)
        for workflow_id in finished:
            current["workflows"].pop(workflow_id, None)
        return current

    try:
        return kv_update(NAMESPACE, KEY, change, default=empty_state(), client=memory)
    except Exception:  # noqa: BLE001 - display only; the next view tries again
        logger.info("finished ticket pauses not forgotten: %s", finished)
        return {**state, "workflows": {k: v for k, v in (state.get("workflows") or {}).items() if k not in finished}}


def held_tasks(memory: Any, *, tenant_id: str = "default") -> list[dict[str, Any]]:
    """Tasks waiting for a resume (``POST /workflow/paused-tasks``); empty when unreadable."""
    try:
        return list((memory.list_paused_tasks(tenant_id=tenant_id) or {}).get("tasks") or [])
    except Exception:  # noqa: BLE001 - display only
        return []


def view(state: dict[str, Any], held: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The console's view of every pause: global, programs and tickets, with their held tasks."""
    held = list(held or [])
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for task in held:
        by_workflow.setdefault(str(task.get("workflow_id") or ""), []).append(task)
    return {
        "global": global_entry(state),
        "programs": [{"program_id": pid, **entry} for pid, entry in sorted((state.get("programs") or {}).items())],
        "workflows": [{"workflow_id": wid, **entry, "held": by_workflow.get(wid, [])}
                      for wid, entry in sorted((state.get("workflows") or {}).items())],
        "held": held,
    }
