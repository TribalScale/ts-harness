"""Record model usage in ``llm_usage`` (spend, budgets) and ``agent_token_usage`` when calls go direct.

With the metering proxy enabled the proxy writes ``llm_usage`` itself, so nothing is recorded here.
Recording never raises: a database problem is logged and the model call carries on.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Mapping

from ts_harness.llm_proxy.metering import ModelPrice, Usage, load_proxy_settings, price

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: Any = None
_pool_failed = False
_pricing: dict[str, ModelPrice] | None = None


def _db_url() -> str:
    return str(os.environ.get("LLM_USAGE_DB_URL") or os.environ.get("TS_HARNESS_SESSION_DB_URL") or "").strip()


def _connection_pool() -> Any:
    global _pool, _pool_failed
    if _pool is not None or _pool_failed:
        return _pool
    with _lock:
        if _pool is None and not _pool_failed:
            url = _db_url()
            if not url:
                _pool_failed = True
                return None
            try:
                from psycopg_pool import ConnectionPool

                _pool = ConnectionPool(url, min_size=0, max_size=4, open=True, timeout=5)
            except Exception as exc:  # noqa: BLE001
                _pool_failed = True
                logger.warning("llm usage recording disabled: %s", exc)
    return _pool


def pricing() -> dict[str, ModelPrice]:
    global _pricing
    if _pricing is None:
        try:
            _pricing = load_proxy_settings(os.environ.get("TS_HARNESS_CONFIG"))["pricing"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_pricing unreadable: %s", exc)
            _pricing = {}
    return _pricing


def recording_enabled() -> bool:
    from ts_harness.llm_proxy.tokens import proxy_enabled

    return not proxy_enabled() and bool(_db_url())


def record_usage(
    scope: Mapping[str, Any] | None,
    *,
    provider: str,
    model: str,
    usage: Usage,
    cost_usd: float | None = None,
    session_id: str | None = None,
    status: int = 200,
    connect: Any = None,
) -> bool:
    """One ``llm_usage`` row and one ``agent_token_usage`` row. ``cost_usd`` defaults to ``llm_pricing``."""
    if usage.empty:
        return False
    if connect is None:
        if not recording_enabled():
            return False
        pool = _connection_pool()
        if pool is None:
            return False
        connect = pool.connection
    claims = {key: str(value) for key, value in (scope or {}).items() if value not in (None, "")}
    cost = price(pricing(), provider, model, usage) if cost_usd is None else float(cost_usd)
    try:
        from ts_harness.llm_proxy.store import UsageStore

        UsageStore(connect).record(claims, provider=provider, model=model, usage=usage, cost_usd=cost, status=status)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_token_usage (agent_id, provider, model, input_tokens, output_tokens, session_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    claims.get("agent") or "unknown",
                    provider,
                    model,
                    usage.input_tokens + usage.cache_read_tokens,
                    usage.output_tokens,
                    session_id or claims.get("task") or claims.get("workflow"),
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm usage not recorded for %s/%s: %s", provider, model, exc)
        return False
