"""Team and program LLM budgets: config sync, proxy admission and the alert job.

The numbers live in Postgres (migration 041): spend from ``llm_usage``, the
effective budget from ``team_monthly_budget``. At 100% the LLM proxy refuses
the team's calls and the claim function holds its agent and coding tasks.

Programs (migration 051) have a total budget (``program_budget``, ``program_spend``).
At 100% ``budgets.program_action`` applies: ``downgrade`` (see ``program_downgrade_note``,
used when a coding job is built) or ``stop`` (``claim_coding_jobs`` holds the program's jobs).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

ADMISSION_CACHE_SECONDS = 30


def sync_budget_settings(memory: Any, config: Any, *, tenant_id: str = "default") -> dict[str, Any]:
    """Copy the ``budgets`` config into ``budget_settings`` so SQL sees the same defaults."""
    return memory.put_budget_settings(tenant_id=tenant_id, **config.budgets.settings_payload())


def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


def proxy_admission(
    connect: Callable[[], Any], *, cache_seconds: float = ADMISSION_CACHE_SECONDS, clock: Callable[[], float] = time.monotonic
) -> Callable[[Mapping[str, str]], str | None]:
    """``admit(claims)`` for the LLM proxy: a refusal message when the caller's team reached its budget.

    The team comes from the token, else from its task. Answers are cached per
    team for ``cache_seconds``; database errors admit the call.
    """
    cache: dict[tuple[str, str, str], tuple[float, str | None]] = {}
    lock = threading.Lock()

    def admit(claims: Mapping[str, str]) -> str | None:
        tenant = claims.get("tenant") or "default"
        key = (tenant, claims.get("team") or "", "" if claims.get("team") else claims.get("task") or "")
        if not key[1] and not key[2]:
            return None
        now = clock()
        with lock:
            hit = cache.get(key)
        if hit and now - hit[0] < cache_seconds:
            return hit[1]
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT team, team_budget_reached(%(tenant)s, team) AS reached
                      FROM (SELECT COALESCE(%(team)s, (SELECT team_id::text FROM workflow_tasks
                                                       WHERE id = %(task)s::uuid)) AS team) scoped
                    """,
                    {"tenant": tenant, "team": claims.get("team") or None, "task": _uuid_or_none(claims.get("task"))},
                )
                row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("budget admission unavailable: %s", exc)
            return None
        team, reached = (row["team"], row["reached"]) if isinstance(row, Mapping) else (row or (None, False))
        refusal = f"team {team} reached its monthly LLM budget; a manager or admin can raise it" if reached else None
        with lock:
            cache[key] = (now, refusal)
        return refusal

    return admit


def alert_text(alert: Mapping[str, Any], *, currency: str) -> str:
    name = alert.get("display_name") or alert.get("slug")
    spent, budget = alert.get("spent_usd"), alert.get("budget_usd")
    if alert.get("level") == "reached":
        text = (
            f"*{name}* reached its monthly LLM budget ({spent:,.2f} of {budget:,.2f} {currency}). "
            "Agent and coding work for the team is on hold until a manager or admin raises it on the Budgets page."
        )
    else:
        text = f"*{name}* has used {alert.get('percent')}% of its monthly LLM budget ({spent:,.2f} of {budget:,.2f} {currency})."
    return text


def budget_alerts(*, tenant_id: str = "default", memory: Any = None, config: Any = None, notifier: Any = None) -> dict[str, Any]:
    """System job: sync settings, then tell the ops channel about teams that crossed the warning or the budget."""
    try:
        if memory is None:
            from ts_harness.memory.client import get_memory_client

            memory = get_memory_client()
        if config is None:
            from ts_harness.gateway.server import get_runtime

            config = get_runtime().config
        sync_budget_settings(memory, config, tenant_id=tenant_id)
        due = memory.claim_budget_alerts(tenant_id=tenant_id).get("alerts") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("budget alerts failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    channel = config.budgets.notify_channel or os.environ.get("SLACK_OPS_CHANNEL", "")
    if notifier is None:
        from ts_harness.notifications.slack import SlackNotifier

        notifier = SlackNotifier()
    sent = 0
    for alert in due:
        result = notifier.post_thread_text(text=alert_text(alert, currency=config.budgets.currency), channel=channel or None)
        sent += 1 if result.get("ok") else 0
        logger.info("team budget %s team=%s percent=%s", alert.get("level"), alert.get("slug"), alert.get("percent"))
    result = {"status": "ok", "alerts": [{"team": a.get("slug"), "level": a.get("level")} for a in due], "sent": sent}
    result["programs"] = program_budget_alerts(memory, notifier, channel=channel, tenant_id=tenant_id)
    return result


# Program budgets: a total per delivery program (migration 051). At warn_at_percent the program's
# thread and the ops channel hear it once; at 100% ``budgets.program_action`` applies.

ACTION_TEXT = {
    "downgrade": (
        "revisions, complex first runs and pre-push reviews use the default coding model instead of the stronger one"
    ),
    "stop": "new coding jobs for its tickets wait",
}


def program_alert_text(alert: Mapping[str, Any]) -> str:
    name, currency = alert.get("name") or alert.get("id"), alert.get("currency") or "USD"
    spent, budget = float(alert.get("spent_usd") or 0), float(alert.get("budget_usd") or 0)
    action = str(alert.get("action") or "downgrade")
    amounts = f"{spent:,.2f} of {budget:,.2f} {currency}"
    if alert.get("level") == "reached":
        return (
            f"Program *{name}* reached its LLM budget ({amounts}). Action taken: *{action}*: "
            f"{ACTION_TEXT.get(action, action)} until an admin or the team's manager raises the budget on the program page."
        )
    return (
        f"Program *{name}* has used {alert.get('percent')}% of its LLM budget ({amounts}). "
        f"At 100%, {ACTION_TEXT.get(action, action)}."
    )


def _slack_thread(source_channel: Any) -> tuple[str, str] | None:
    from ts_harness.gateway.workflow_presentation import bound_channel

    parsed = bound_channel(str(source_channel or ""))
    if parsed is None or parsed[0] != "slack":
        return None
    return parsed[1], parsed[2]


def _workflow_thread(memory: Any, workflow_id: str) -> tuple[str, str] | None:
    """The Slack thread of a ticket's workflow, else of the workflow that started it."""
    seen: set[str] = set()
    while workflow_id and workflow_id not in seen:
        seen.add(workflow_id)
        workflow = (memory.get_workflow(workflow_id) or {}).get("workflow") or {}
        thread = _slack_thread(workflow.get("source_channel"))
        if thread:
            return thread
        workflow_id = str(workflow.get("parent_workflow_id") or "")
    return None


def program_budget_alerts(memory: Any, notifier: Any, *, channel: str = "", tenant_id: str = "default") -> dict[str, Any]:
    """Post program warnings and budget-reached notices, and the ticket notes of held or downgraded jobs."""
    try:
        data = memory.claim_program_budget_alerts(tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("program budget alerts failed: %s", exc)
        return {"status": "error", "error": str(exc)}
    posted = 0
    alerts = data.get("alerts") or []
    for alert in alerts:
        text = program_alert_text(alert)
        thread = _slack_thread(alert.get("source_channel"))
        if thread:
            posted += 1 if notifier.post_thread_text(text=text, channel=thread[0], thread_ts=thread[1]).get("ok") else 0
        if channel:
            posted += 1 if notifier.post_thread_text(text=text, channel=channel).get("ok") else 0
        logger.info("program budget %s program=%s percent=%s", alert.get("level"), alert.get("id"), alert.get("percent"))
    notes = data.get("notes") or []
    for note in notes:
        try:
            thread = _workflow_thread(memory, str(note.get("workflow_id") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ticket thread lookup failed job=%s: %s", note.get("id"), exc)
            thread = None
        if thread:
            label = f"{note['work_order_id']}: " if note.get("work_order_id") else ""
            text = f"{label}{note.get('budget_note')}"
            posted += 1 if notifier.post_thread_text(text=text, channel=thread[0], thread_ts=thread[1]).get("ok") else 0
    return {
        "alerts": [{"program": a.get("id"), "level": a.get("level"), "action": a.get("action")} for a in alerts],
        "notes": len(notes),
        "posted": posted,
    }


def program_downgrade_note(program_id: str | None, *, memory: Any = None) -> str | None:
    """Why a coding run of this program must not use the stronger model, or None.

    Set when the program reached its budget and ``budgets.program_action`` is ``downgrade``.
    A failed lookup returns None: the run keeps its normal models.
    """
    if not program_id:
        return None
    try:
        if memory is None:
            from ts_harness.memory.client import get_memory_client

            memory = get_memory_client()
        program = memory.get_program_budget(str(program_id)).get("program") or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("program budget lookup failed program=%s: %s", program_id, exc)
        return None
    if program.get("status") != "reached" or program.get("action") != "downgrade":
        return None
    currency = program.get("currency") or "USD"
    return (
        f"Program {program.get('name') or program_id} reached its LLM budget "
        f"({float(program.get('spent_usd') or 0):,.2f} of {float(program.get('budget_usd') or 0):,.2f} {currency}), "
        "so this run uses the default coding model for coding and the pre-push review instead of the stronger one. "
        "An admin or the team's manager can raise the budget on the program page."
    )
