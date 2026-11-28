"""The hourly improvement job: evaluate proposals offline, start canaries, apply the stop rule.

Each run (:func:`run_loop`, behind the ``improvement_loop`` system job):

1. **Notes**: a ``knowledge_note`` proposal follows its note: promoted when a person approves the
   note in the Inbox, rejected when they reject or archive it.
2. **Offline check**: at most one proposal with an offline check (a pre-push reviewer
   ``skill_edit``) whose target has no live canary is replayed (``replay.evaluate``); replay cases
   are built first when there are too few. Passed -> the loop starts its canary
   (``canary.start``); failed -> rejected; inconclusive (too few cases, the budget ran out) ->
   back to ``proposed``, tried again after ``RETRY_DAYS``. A proposal left in ``evaluating`` by a
   crashed worker goes back to ``proposed`` after ``STALE_EVALUATING_HOURS``.
3. **Stop rule**: ``canary.check`` on every live canary.
4. **Retention**: replay cases older than 90 days are deleted, once a day.

``TS_HARNESS_IMPROVEMENT_LOOP=off`` turns it off. Nothing here touches a delivery run.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ts_harness.improvement import canary as cn
from ts_harness.improvement import proposals as pp
from ts_harness.improvement import replay as rp
from ts_harness.improvement.budget import ImprovementBudget

logger = logging.getLogger(__name__)

RETRY_DAYS = 7
STALE_EVALUATING_HOURS = 6
STATE_NAMESPACE = "improvement-loop"
PRUNE_KEY = "replay-prune"


def enabled() -> bool:
    return (os.environ.get("TS_HARNESS_IMPROVEMENT_LOOP") or "on").strip().lower() not in {"0", "off", "false", "no"}


def _parse(value: Any) -> datetime | None:
    return cn._parse(value)


def sync_notes(memory: Any, rows: list[dict[str, Any]], *, tenant_id: str) -> list[dict[str, Any]]:
    """Knowledge-note proposals follow their note's status."""
    moved = []
    for proposal in rows:
        if proposal.get("type") != "knowledge_note" or proposal.get("status") != "proposed":
            continue
        note_id = str((proposal.get("change") or {}).get("note_id") or "")
        if not note_id or not hasattr(memory, "get_knowledge_note"):
            continue
        try:
            note = memory.get_knowledge_note(note_id) or {}
        except Exception:  # noqa: BLE001 - checked again next hour
            continue
        status = str(note.get("status") or "")
        target = "promoted" if status == "active" else "rejected" if status in {"rejected", "archived"} else ""
        if not target:
            continue
        actor = str(note.get("approved_by") or note.get("updated_by") or "a person")
        try:
            pp.move(memory, proposal, target, actor=actor, reason=f"the knowledge note was {status}", tenant_id=tenant_id,
                    promotion={"method": "knowledge_note", "note_id": note_id} if target == "promoted" else None)
            moved.append({"id": proposal["id"], "status": target})
        except Exception as exc:  # noqa: BLE001
            logger.info("note proposal %s not synced: %s", proposal.get("id"), exc)
    return moved


def _due(proposal: dict[str, Any], now: datetime) -> bool:
    """A proposal with an offline check that should be (re)evaluated now."""
    if proposal.get("status") != "proposed" or (proposal.get("change") or {}).get("offline_check") != pp.PRE_PUSH_TARGET:
        return False
    evaluation = proposal.get("evaluation") if isinstance(proposal.get("evaluation"), dict) else None
    if not evaluation:
        return True
    if evaluation.get("status") == pp.EVAL_PASSED:
        return True  # passed earlier, waited for the target's slot
    at = _parse(evaluation.get("at"))
    return at is None or now - at >= timedelta(days=RETRY_DAYS)


def _enough_cases(memory: Any, tenant_id: str) -> bool:
    cases = memory.list_replay_cases(tenant_id=tenant_id, kind=rp.KIND, limit=200, inputs=False)
    blocks = sum(1 for c in cases if c.get("truth_verdict") == "block")
    passes = sum(1 for c in cases if c.get("truth_verdict") == "pass")
    half = rp.DEFAULT_CASES // 2
    return blocks >= half and passes >= half


def evaluate_next(memory: Any, rows: list[dict[str, Any]], *, tenant_id: str, now: datetime,
                  model_call: Callable[[str], str] | None = None, budget: Any = None,
                  reader: Any = None, runs: Any = None, github: Any = None) -> dict[str, Any] | None:
    """Replay one due proposal and act on the result; None when nothing was due."""
    from ts_harness.improvement import llm

    live_targets = {p.get("target") for p in rows if p.get("status") in {"canary", "awaiting_approval"}}
    due = sorted((p for p in rows if _due(p, now) and p.get("target") not in live_targets), key=lambda p: int(p["id"]))
    if not due:
        return None
    proposal = due[0]
    budget = budget or ImprovementBudget(tenant_id=tenant_id, memory=memory)
    if not budget.can_spend(rp.CASE_ESTIMATE_USD):
        return {"id": proposal["id"], "status": "paused", "reason": "the improvement budget is used up"}
    call = model_call or llm.metered_call("replay", ref=pp.variant_name(proposal), budget=budget,
                                          estimate_usd=rp.CASE_ESTIMATE_USD / 2, tenant_id=tenant_id)
    if call is None:
        return {"id": proposal["id"], "status": "skipped", "reason": "no improvement model configured"}
    evaluating = pp.move(memory, proposal, "evaluating", reason="offline replay started", tenant_id=tenant_id)
    try:
        built = None
        if not _enough_cases(memory, tenant_id):
            built = rp.build_cases(memory, tenant_id=tenant_id, now=now, runs=runs, github=github)
        model = str(getattr(call, "model", "") or "")
        result = rp.evaluate(evaluating, memory, model_call=call, model=model, tenant_id=tenant_id, reader=reader,
                             budget=budget, now=now)
        if built is not None:
            result["cases_built"] = built
    except Exception as exc:  # noqa: BLE001 - back to proposed, tried again later
        logger.warning("replay of proposal %s failed: %s", proposal.get("id"), exc)
        result = {"kind": "replay", "status": pp.EVAL_INCONCLUSIVE, "reason": f"replay error: {str(exc)[:200]}",
                  "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "cost_usd": round(float(getattr(call, "spent", 0.0) or 0.0), 4)}
    cost = float(result.get("cost_usd") or 0.0)
    status = result["status"]
    if status == pp.EVAL_PASSED:
        recorded = pp.annotate(memory, evaluating, tenant_id=tenant_id, evaluation=result, add_cost_usd=cost)
        moved = cn.start(memory, recorded, tenant_id=tenant_id, now=now)
    elif status == pp.EVAL_FAILED:
        moved = pp.move(memory, evaluating, "rejected", reason=f"offline check failed: {result.get('reason')}",
                        tenant_id=tenant_id, evaluation=result, add_cost_usd=cost)
    else:
        moved = pp.move(memory, evaluating, "proposed", reason=f"offline check inconclusive: {result.get('reason')}",
                        tenant_id=tenant_id, evaluation=result, add_cost_usd=cost)
    return {"id": proposal["id"], "evaluation": status, "reason": result.get("reason"), "status": moved.get("status"),
            "cost_usd": cost}


def release_stale(memory: Any, rows: list[dict[str, Any]], *, tenant_id: str, now: datetime) -> list[int]:
    released = []
    for proposal in rows:
        if proposal.get("status") != "evaluating":
            continue
        updated = _parse(proposal.get("updated_at"))
        if updated is not None and now - updated < timedelta(hours=STALE_EVALUATING_HOURS):
            continue
        try:
            pp.move(memory, proposal, "proposed", reason="the offline replay did not finish; will try again",
                    tenant_id=tenant_id)
            released.append(int(proposal["id"]))
        except Exception:  # noqa: BLE001
            continue
    return released


def _prune_due(memory: Any, now: datetime) -> bool:
    try:
        current = memory.kv_get(STATE_NAMESPACE, PRUNE_KEY) or {}
        value = current.get("value") if isinstance(current.get("value"), dict) else {}
        last = _parse(value.get("at"))
        if last is not None and now - last < timedelta(days=1):
            return False
        memory.kv_put(STATE_NAMESPACE, PRUNE_KEY, {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
                      expected_version=int(current.get("version") or 0))
        return True
    except Exception:  # noqa: BLE001 - another worker pruned
        return False


def run_loop(*, memory: Any = None, tenant_id: str = "default", now: datetime | None = None,
             model_call: Callable[[str], str] | None = None, metrics: Callable[..., dict[str, Any]] | None = None,
             budget: Any = None, reader: Any = None, runs: Any = None, github: Any = None) -> dict[str, Any]:
    """System job; see the module docstring."""
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    if not enabled():
        return {"status": "skipped", "reason": "TS_HARNESS_IMPROVEMENT_LOOP is off"}
    now = now or datetime.now(timezone.utc)
    rows = pp.list_open(memory, tenant_id=tenant_id)
    result: dict[str, Any] = {"status": "ok"}
    result["notes"] = sync_notes(memory, rows, tenant_id=tenant_id)
    result["released"] = release_stale(memory, rows, tenant_id=tenant_id, now=now)
    if result["notes"] or result["released"]:
        rows = pp.list_open(memory, tenant_id=tenant_id)
    if cn.enabled():
        result["evaluated"] = evaluate_next(memory, rows, tenant_id=tenant_id, now=now, model_call=model_call,
                                            budget=budget, reader=reader, runs=runs, github=github)
    result["canaries"] = cn.check(memory, tenant_id=tenant_id, now=now, metrics=metrics, reader=reader)
    if _prune_due(memory, now):
        result["pruned"] = rp.prune(memory, tenant_id=tenant_id)
    return result
