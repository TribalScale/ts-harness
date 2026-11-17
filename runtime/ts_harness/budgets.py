"""Team LLM budgets: config sync, proxy admission and the alert job.

The numbers live in Postgres (migration 041): spend from ``llm_usage``, the
effective budget from ``team_monthly_budget``. At 100% the LLM proxy refuses
the team's calls and the claim function holds its agent and coding tasks.
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
    return {"status": "ok", "alerts": [{"team": a.get("slug"), "level": a.get("level")} for a in due], "sent": sent}
