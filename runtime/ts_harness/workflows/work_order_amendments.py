"""Amend a running work order: a person's ``## Scope amendment`` that every later step reads.

A work order sometimes needs a person to change it while it is being delivered: QA keeps
blocking on parts another ticket owns, or the data state the frame draws needs a different
seed. PR ``/revise`` comments only act while the PR waits for its merge, and QA blocks
before that. An amendment works at any stage:

- It appends ``## Scope amendment (by <person>, <date>)`` and the text to the work order.
  The work order is the ``work_order_body`` of the earliest succeeded task output that
  carries it (``prepare_ticket_delivery`` or ``prepare_work_order_delivery``), which is where
  ``$input.work_order_body`` resolves from (``workflows/expressions.py``), so coding, code
  review and QA all read it from then on. Tasks that already hold a copy of the body and have
  not run yet get it patched in, the program artifacts too when they carry the body, and the
  runner re-applies any missing amendment just before a step that carries the body runs.
- The QA, code review and coding instructions say a scope amendment from a person always
  wins over the rest of the work order.
- History: ``runtime_kv`` namespace ``work-order-amendments``, key ``workflow:<id>``, keeps
  the original work order text and every amendment (who, when, where from, the text, the PR
  comment id). Comment ids make ``/amend`` idempotent: a comment seen by both the webhook and
  the poller is applied once.

Three ways in: the console (Amend work order on a ticket card or the program board), the
admin API (``POST /admin/workflows/amend``, ``GET /admin/workflows/{id}/amendments``), and a
``/amend <text>`` PR comment from someone with write access (``gateway/pr_commands.py``).
Each posts a short acknowledgement on the PR and in the bound chat.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

NAMESPACE = "work-order-amendments"
HEADING = "## Scope amendment"
MAX_TEXT = 4000
MAX_AMENDMENTS = 30
PATCHABLE = frozenset({"pending", "ready", "blocked", "waiting"})
DELIVERY_TYPES = frozenset({"implement_jira_ticket", "implement_ticket", "deliver_work_order", "bug_work_order"})


class AmendmentError(ValueError):
    """The work order can't be amended (empty text, no work order yet, finished run)."""


def _key(workflow_id: str) -> str:
    return f"workflow:{workflow_id}"


def _marker(amendment_id: str) -> str:
    return f"<!-- amendment:{amendment_id} -->"


def amendment_section(amendment: dict[str, Any]) -> str:
    """The markdown appended to the work order for one amendment."""
    return (
        f"{HEADING} (by {amendment.get('person') or 'a person'}, {amendment.get('date') or ''})\n"
        f"{_marker(str(amendment.get('id') or ''))}\n\n"
        "This amends the work order and wins over anything above it that it contradicts.\n\n"
        f"{str(amendment.get('text') or '').strip()}\n"
    )


def _append(body: str, amendment: dict[str, Any]) -> str:
    return str(body or "").rstrip() + "\n\n" + amendment_section(amendment)


def with_amendments(body: str, amendments: list[dict[str, Any]]) -> str:
    """``body`` with every amendment it does not carry yet appended, oldest first."""
    for amendment in amendments:
        if _marker(str(amendment.get("id") or "")) not in str(body or ""):
            body = _append(body, amendment)
    return body


def _output(task: dict[str, Any]) -> dict[str, Any]:
    return dict(task.get("output")) if isinstance(task.get("output"), dict) else {}


def work_order_source(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The task whose output ``$input.work_order_body`` resolves from (see ``expressions._workflow_handoff_value``)."""
    ordered = sorted(tasks, key=lambda item: str(item.get("created_at") or ""))
    prep = [t for t in ordered if t.get("step_name") == "prepare_work_order_delivery"]
    if prep and prep[-1].get("status") == "succeeded" and _output(prep[-1]).get("work_order_body") is not None:
        return prep[-1]
    for task in ordered:
        if task.get("status") == "succeeded" and _output(task).get("work_order_body") is not None:
            return task
    return None


def _record(memory: Any, workflow_id: str) -> dict[str, Any]:
    value = (memory.kv_get(NAMESPACE, _key(workflow_id)) or {}).get("value")
    return value if isinstance(value, dict) else {}


def amendment_history(memory: Any, workflow_id: str) -> dict[str, Any]:
    """``{original, amendments, current}`` for the workflow's work order."""
    record = _record(memory, workflow_id)
    payload = memory.get_workflow(workflow_id) or {}
    source = work_order_source(list(payload.get("tasks") or []))
    current = str(_output(source).get("work_order_body") or "") if source else ""
    return {
        "workflow_id": workflow_id,
        "original": str(record.get("original") or current),
        "amendments": list(record.get("amendments") or []),
        "current": current,
    }


def with_recorded_amendments(memory: Any, workflow_id: str, task_input: dict[str, Any]) -> dict[str, Any]:
    """``task_input`` with any recorded amendment its ``work_order_body`` lacks; the same object when none."""
    body = str(task_input.get("work_order_body") or "")
    if not body or not workflow_id:
        return task_input
    amendments = [a for a in _record(memory, workflow_id).get("amendments") or [] if isinstance(a, dict)]
    if not amendments:
        return task_input
    updated = with_amendments(body, amendments)
    return task_input if updated == body else {**task_input, "work_order_body": updated}


def _label(workflow: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    from ts_harness.workflows.human_decisions import work_order_label

    try:
        return work_order_label(tasks, workflow)
    except Exception:  # noqa: BLE001
        return str(workflow.get("external_ref") or "the work order")


def amend_work_order(
    memory: Any,
    workflow_id: str,
    text: str,
    *,
    person: str,
    actor: str,
    source: str,
    comment_id: str = "",
    pr_url: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append a scope amendment to the workflow's work order. Idempotent by ``comment_id``.

    ``status`` is ``amended`` or ``duplicate``. Raises ``AmendmentError`` when the text is empty,
    the workflow is not running, or it has no work order yet.
    """
    from ts_harness.storage.kv import kv_update

    text = str(text or "").strip()
    if not text:
        raise AmendmentError("the amendment is empty: say what changes in the work order")
    if len(text) > MAX_TEXT:
        raise AmendmentError(f"the amendment is too long ({len(text)} characters; at most {MAX_TEXT})")
    payload = memory.get_workflow(workflow_id) or {}
    workflow = dict(payload.get("workflow") or {})
    tasks = list(payload.get("tasks") or [])
    if not workflow:
        raise AmendmentError("workflow not found")
    if str(workflow.get("status") or "running") != "running":
        raise AmendmentError(f"the delivery is {workflow.get('status')}; only a running work order can be amended")
    source_task = work_order_source(tasks)
    if source_task is None:
        raise AmendmentError("this delivery has no work order yet; amend it once its first step has finished")
    now = now or datetime.now(timezone.utc)
    current = str(_output(source_task).get("work_order_body") or "")
    amendment = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "person": str(person or actor or "a person"),
        "actor": str(actor or ""),
        "source": source,
        "comment_id": str(comment_id or ""),
        "pr_url": str(pr_url or ""),
        "at": now.isoformat(),
        "date": now.date().isoformat(),
    }
    claimed: dict[str, Any] = {"duplicate": False}

    def claim(value: Any) -> Any:
        state = dict(value or {})
        known = [a for a in state.get("amendments") or [] if isinstance(a, dict)]
        if amendment["comment_id"] and any(a.get("comment_id") == amendment["comment_id"] for a in known):
            claimed["duplicate"] = True
            return state
        if len(known) >= MAX_AMENDMENTS:
            raise AmendmentError(f"this work order already has {MAX_AMENDMENTS} amendments")
        state.setdefault("original", current)
        state["workflow_id"] = workflow_id
        state["amendments"] = [*known, {**amendment, "body_before": current}]
        return state

    kv_update(NAMESPACE, _key(workflow_id), claim, default={}, client=memory)
    label = _label(workflow, tasks)
    if claimed["duplicate"]:
        return {"status": "duplicate", "workflow_id": workflow_id, "work_order": label}
    amended = _append(current, amendment)
    try:
        memory.patch_task_output(
            str(source_task.get("id")), {**_output(source_task), "work_order_body": amended}, actor=actor,
            reason=f"work order amended by {amendment['person']}",
        )
    except Exception:
        # Without the source patched nothing reads the amendment: take the claim back.
        def unclaim(value: Any) -> Any:
            state = dict(value or {})
            state["amendments"] = [a for a in state.get("amendments") or [] if a.get("id") != amendment["id"]]
            return state

        kv_update(NAMESPACE, _key(workflow_id), unclaim, default={}, client=memory)
        raise
    patched = _patch_waiting_tasks(memory, tasks, source_task, amendment, actor=actor)
    _patch_program_artifacts(memory, workflow, amendment, actor=actor)
    return {
        "status": "amended",
        "workflow_id": workflow_id,
        "work_order": label,
        "amendment": amendment,
        "tasks_patched": patched,
    }


def _patch_waiting_tasks(
    memory: Any, tasks: list[dict[str, Any]], source_task: dict[str, Any], amendment: dict[str, Any], *, actor: str
) -> int:
    """Tasks that hold a copy of the body and have not run yet get the amendment too."""
    patched = 0
    for task in tasks:
        if str(task.get("id")) == str(source_task.get("id")) or str(task.get("status") or "") not in PATCHABLE:
            continue
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        body = task_input.get("work_order_body")
        if not isinstance(body, str) or not body.strip():
            continue
        try:
            memory.patch_task_input(str(task.get("id")), {**task_input, "work_order_body": with_amendments(body, [amendment])}, actor=actor)
            patched += 1
        except Exception as exc:  # noqa: BLE001 - the runner re-applies it before the step runs
            logger.warning("amendment not patched into task %s: %s", task.get("id"), exc)
    return patched


def _patch_program_artifacts(memory: Any, workflow: dict[str, Any], amendment: dict[str, Any], *, actor: str) -> None:
    """A program whose artifacts carry this work order's body (they win in ``$input``) gets it too."""
    program_id = str(workflow.get("program_id") or "")
    if not program_id or not hasattr(memory, "get_program_status"):
        return
    try:
        artifacts = (memory.get_program_status(program_id) or {}).get("artifacts") or {}
        body = artifacts.get("work_order_body") if isinstance(artifacts, dict) else None
        if isinstance(body, str) and body.strip():
            memory.patch_program_artifacts(program_id, {"work_order_body": with_amendments(body, [amendment])}, actor=actor)
    except Exception as exc:  # noqa: BLE001 - the runner re-applies it before each step
        logger.warning("amendment not patched into program %s artifacts: %s", program_id, exc)


MAX_HANDLED_COMMENTS = 200


def comment_handled(memory: Any, workflow_id: str, comment_id: str) -> bool:
    """A ``/amend`` comment was already handled (applied, refused or failed) for this delivery."""
    if not comment_id:
        return False
    record = _record(memory, workflow_id)
    if comment_id in (record.get("handled_comments") or []):
        return True
    return any(a.get("comment_id") == comment_id for a in record.get("amendments") or [] if isinstance(a, dict))


def mark_comment_handled(memory: Any, workflow_id: str, comment_id: str) -> None:
    """Remember a handled comment, so the poller never answers it again."""
    from ts_harness.storage.kv import kv_update

    if not comment_id:
        return

    def mark(value: Any) -> Any:
        state = dict(value or {})
        handled = [c for c in state.get("handled_comments") or [] if c != comment_id]
        state["handled_comments"] = [*handled, comment_id][-MAX_HANDLED_COMMENTS:]
        return state

    kv_update(NAMESPACE, _key(workflow_id), mark, default={}, client=memory)


# ── Finding the delivery of a PR, and acknowledging ─────────────────────────


def _norm(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def workflow_pr_url(tasks: list[dict[str, Any]]) -> str:
    """The PR the delivery opened (the latest succeeded task output naming one)."""
    pr_url = ""
    for task in sorted(tasks, key=lambda item: str(item.get("created_at") or "")):
        output = _output(task)
        if task.get("status") == "succeeded" and output.get("pr_url"):
            pr_url = str(output["pr_url"])
    return pr_url


def running_deliveries(memory: Any, *, tenant_id: str = "default") -> list[dict[str, Any]]:
    """``{workflow_id, program_id, pr_url, work_order}`` of every running delivery with a PR."""
    from ts_harness.workflows.live_view import _program_items

    found: dict[str, dict[str, Any]] = {}
    programs = (memory.list_programs(tenant_id=tenant_id, status="running", limit=100) or {}).get("programs") or []
    for program in programs:
        program_id = str(program.get("id") or "")
        if not program_id:
            continue
        for record in _program_items(memory, program_id, running_only=True):
            workflow_id = str(record.get("id") or "")
            if not workflow_id or workflow_id in found:
                continue
            payload = memory.get_workflow(workflow_id) or {}
            tasks = list(payload.get("tasks") or [])
            pr_url = workflow_pr_url(tasks)
            if pr_url:
                found[workflow_id] = {
                    "workflow_id": workflow_id,
                    "program_id": program_id,
                    "pr_url": pr_url,
                    "work_order": _label(dict(payload.get("workflow") or {}), tasks),
                }
    return list(found.values())


def delivery_for_pr(memory: Any, pr_url: str, *, tenant_id: str = "default") -> dict[str, Any] | None:
    """The running delivery whose PR is ``pr_url``, at any stage."""
    for item in running_deliveries(memory, tenant_id=tenant_id):
        if _norm(item["pr_url"]) == _norm(pr_url):
            return item
    return None


def acknowledgement(result: dict[str, Any]) -> str:
    amendment = result.get("amendment") or {}
    return (
        f"Amended {result.get('work_order') or 'the work order'}: added a scope amendment by "
        f"{amendment.get('person') or 'a person'}. Coding, code review and QA read it from their next run."
    )


def acknowledge(
    workflow_id: str,
    text: str,
    *,
    pr_url: str = "",
    tenant_id: str = "default",
    actor: str = "",
    engine: Any = None,
    reply: Any = None,
) -> dict[str, bool]:
    """Say it on the PR and in the delivery's bound chat thread (Slack or web). Best effort."""
    said = {"pr": False, "chat": False}
    if pr_url:
        from ts_harness.gateway.pr_commands import _reply_on_pr

        try:
            (reply or _reply_on_pr)(pr_url, text)
            said["pr"] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("amendment acknowledgement not posted on %s: %s", pr_url, exc)
    try:
        if engine is None:
            from ts_harness.workflows.engine import get_workflow_engine

            engine = get_workflow_engine()
        from ts_harness.workflows.hooks.notify_bound_channel import handle as notify_bound

        payload = engine.client.get_workflow(workflow_id) or {}
        workflow = dict(payload.get("workflow") or {})
        tasks = sorted(payload.get("tasks") or [], key=lambda item: str(item.get("created_at") or ""))
        ctx = engine._context(
            workflow_id=workflow_id,
            workflow_type=str(workflow.get("workflow_type") or ""),
            tenant_id=tenant_id,
            task=tasks[-1] if tasks else {},
            actor=actor or "workflow-runner",
            runner_event="retried",
            extras={},
        )
        ctx.extras["channel_text"] = text
        said["chat"] = str((notify_bound(ctx) or {}).get("status") or "") == "notified"
    except Exception as exc:  # noqa: BLE001
        logger.warning("amendment acknowledgement not posted in chat for %s: %s", workflow_id, exc)
    return said
