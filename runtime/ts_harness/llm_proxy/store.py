"""Postgres for the proxy: ``llm_usage`` rows and per-model request buckets (migration 040)."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Mapping

from ts_harness.llm_proxy.metering import Usage, match_model

logger = logging.getLogger(__name__)



def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


class UsageStore:
    def __init__(self, connect: Callable[[], Any]):
        self.connect = connect

    def record(
        self,
        claims: Mapping[str, str],
        *,
        provider: str,
        model: str,
        usage: Usage,
        cost_usd: float,
        status: int,
    ) -> None:
        """One row per call. Team and program fall back to the task and workflow when the token lacks them."""
        params = {
            "tenant": claims.get("tenant") or "default",
            "team": claims.get("team"),
            "project": claims.get("project"),
            "program": claims.get("program"),
            "workflow": claims.get("workflow"),
            "task": claims.get("task"),
            "actor": claims.get("user") or claims.get("agent"),
            "provider": provider,
            "model": model,
            "input": usage.input_tokens + usage.cache_read_tokens,
            "output": usage.output_tokens,
            "cost": cost_usd,
            "status": status,
            "task_uuid": _uuid_or_none(claims.get("task")),
            "workflow_uuid": _uuid_or_none(claims.get("workflow")),
        }
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_usage (tenant_id, team_id, project_id, program_id, workflow_id, task_id, actor,
                                       provider, model, input_tokens, output_tokens, cost_usd, status)
                VALUES (
                    %(tenant)s,
                    COALESCE(%(team)s, (SELECT team_id::text FROM workflow_tasks WHERE id = %(task_uuid)s::uuid)),
                    %(project)s,
                    COALESCE(%(program)s, workflow_program_id(%(workflow_uuid)s::uuid)::text),
                    %(workflow)s, %(task)s, %(actor)s, %(provider)s, %(model)s,
                    %(input)s, %(output)s, %(cost)s, %(status)s
                )
                """,
                params,
            )


class ModelRateLimiter:
    """Token buckets per ``llm_rate_limits`` entry, shared by every proxy replica.

    ``{"anthropic/*": {"requests_per_minute": 120, "burst": 20}}``; models
    without an entry are not limited. When the database fails the call goes through.
    """

    def __init__(self, connect: Callable[[], Any] | None, limits: Mapping[str, Any]):
        self.connect = connect
        self.limits = dict(limits or {})

    def allow(self, provider: str, model: str) -> bool:
        matched = match_model(self.limits, provider, model)
        if matched is None or self.connect is None:
            return True
        key, rule = matched
        rpm = float((rule or {}).get("requests_per_minute") or 0)
        if rpm <= 0:
            return True
        cap = float((rule or {}).get("burst") or rpm)
        params = {"key": key, "cap": cap, "rate": rpm / 60.0}
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO llm_rate_buckets (key, tokens, updated_at) VALUES (%(key)s, %(cap)s, now()) "
                    "ON CONFLICT (key) DO NOTHING",
                    params,
                )
                cur.execute(
                    """
                    UPDATE llm_rate_buckets
                       SET tokens = LEAST(%(cap)s, tokens + extract(epoch FROM now() - updated_at) * %(rate)s) - 1,
                           updated_at = now()
                     WHERE key = %(key)s
                       AND LEAST(%(cap)s, tokens + extract(epoch FROM now() - updated_at) * %(rate)s) >= 1
                    RETURNING tokens
                    """,
                    params,
                )
                return cur.fetchone() is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm rate limit unavailable for %s: %s", key, exc)
            return True
