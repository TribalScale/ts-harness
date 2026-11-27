"""The self-improvement loop's monthly model-cost cap, apart from delivery budgets.

Improvement work (failure digests, proposals, replays) spends from its own monthly budget: the
cap is ``budgets.improvement_monthly_usd`` in the runtime config (default 150 USD), or
``TS_HARNESS_IMPROVEMENT_MONTHLY_USD`` when set. The window is the calendar month in UTC. When the
month's spend reaches the cap, improvement work pauses until the next month; delivery is never
affected (its calls are not charged here, and nothing here holds a delivery job).

The ledger is the ``improvement_spend`` table (migration 055) behind the memory API
(``GET /budgets/improvement``, ``POST /budgets/improvement/charges``): durable, shared by every
replica, and summed per month in SQL. Calls also land in ``llm_usage`` like every model call
(scope ``agent=self-improvement``, no team or program), so they never count against a team or
program budget.

Use it like this::

    budget = ImprovementBudget()
    if not budget.can_spend(0.50):
        return  # paused until next month
    with budget.track("weekly_digest", ref=digest_id, model=model) as call:
        text = call(prompt)            # one model call, charged at its actual cost

    budget.charge(0.12, "replay", "replay-42")    # a cost measured elsewhere

``can_spend`` is advisory (two workers may both pass it; the overrun is at most one call each).
Nothing here raises into the caller on a ledger outage except :class:`BudgetExhausted` from
:meth:`ImprovementBudget.track` / :meth:`ImprovementBudget.model_call` when the cap is reached,
and ``can_spend`` answers ``False`` when the ledger cannot be read (fail closed: improvement work
waits, delivery carries on).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

DEFAULT_MONTHLY_USD = 150.0
CAP_ENV = "TS_HARNESS_IMPROVEMENT_MONTHLY_USD"
USAGE_AGENT = "self-improvement"


class BudgetExhausted(RuntimeError):
    """The improvement budget for this month is used up (or cannot be read)."""


def current_month(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def monthly_cap(config: Any = None) -> float:
    """The cap in USD: the env override, then ``budgets.improvement_monthly_usd``, then 150."""
    raw = os.environ.get(CAP_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            pass
        logger.warning("%s is not a non-negative number; using the configured cap", CAP_ENV)
    if config is None:
        from ts_harness.improvement.versions import _runtime_config

        config = _runtime_config()
    budgets = getattr(config, "budgets", None)
    value = getattr(budgets, "improvement_monthly_usd", None)
    return float(value) if isinstance(value, (int, float)) and value >= 0 else DEFAULT_MONTHLY_USD


@dataclass
class ImprovementBudget:
    """The month's improvement budget for a tenant. ``memory`` defaults to the memory API client."""

    tenant_id: str = "default"
    memory: Any = None
    cap_usd: float | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    def _memory(self) -> Any:
        if self.memory is None:
            from ts_harness.memory.client import get_memory_client

            self.memory = get_memory_client()
        return self.memory

    def cap(self) -> float:
        return float(self.cap_usd) if self.cap_usd is not None else monthly_cap()

    def month(self) -> str:
        return current_month(self.now())

    def status(self) -> dict[str, Any]:
        """``{month, cap_usd, spent_usd, remaining_usd, paused, by_purpose, recent, available}``.

        ``available`` is False when the ledger could not be read (then ``spent_usd`` is ``None``
        and ``paused`` is True: improvement work waits rather than spend blind).
        """
        cap = self.cap()
        month = self.month()
        try:
            data = self._memory().improvement_spend(tenant_id=self.tenant_id, month=month) or {}
            spent = round(float(data.get("spent_usd") or 0), 4)
        except Exception as exc:  # noqa: BLE001
            logger.warning("improvement budget unreadable: %s", exc)
            return {"month": month, "cap_usd": cap, "spent_usd": None, "remaining_usd": None, "paused": True,
                    "by_purpose": {}, "recent": [], "available": False}
        remaining = round(max(0.0, cap - spent), 4)
        return {
            "month": month,
            "cap_usd": cap,
            "spent_usd": spent,
            "remaining_usd": remaining,
            "paused": remaining <= 0,
            "by_purpose": dict(data.get("by_purpose") or {}),
            "recent": list(data.get("recent") or [])[:20],
            "available": True,
        }

    def remaining(self) -> float:
        """USD left this month; 0 when the ledger cannot be read."""
        return float(self.status()["remaining_usd"] or 0.0)

    def can_spend(self, estimate_usd: float = 0.0) -> bool:
        """Whether improvement work may start something estimated at ``estimate_usd``."""
        state = self.status()
        if not state["available"]:
            return False
        estimate = max(0.0, float(estimate_usd or 0))
        return state["remaining_usd"] > 0 and estimate <= state["remaining_usd"]

    def charge(self, usd: float, purpose: str, ref: str = "", *, model: str = "", input_tokens: int | None = None,
               output_tokens: int | None = None, actor: str = USAGE_AGENT) -> dict[str, Any] | None:
        """Record spend already incurred. Returns the month's spend after it, or ``None`` when it
        could not be recorded (logged; the caller's work stands)."""
        body: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "cost_usd": round(max(0.0, float(usd or 0)), 6),
            "purpose": str(purpose or "").strip()[:80] or "unspecified",
            "ref": str(ref or "")[:200],
            "model": str(model or "")[:200],
            "actor": actor,
        }
        if input_tokens is not None:
            body["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            body["output_tokens"] = int(output_tokens)
        try:
            return self._memory().charge_improvement(**body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("improvement charge not recorded (%s %s USD): %s", purpose, usd, exc)
            return None

    @contextmanager
    def track(self, purpose: str, *, ref: str = "", model: str, estimate_usd: float = 0.0,
              provider_factory: Callable[[str], Any] | None = None) -> Iterator[Callable[[str], str]]:
        """A model call charged at its actual cost: ``with budget.track(...) as call: text = call(prompt)``.

        Raises :class:`BudgetExhausted` before any call when the budget cannot cover ``estimate_usd``.
        Every call made inside the block is priced from its reported usage (``llm_pricing``) and
        charged once when the block ends, even if it raised.
        """
        if not self.can_spend(estimate_usd):
            raise BudgetExhausted(f"improvement budget for {self.month()} is used up or unreadable")
        totals = {"cost": 0.0, "input": 0, "output": 0, "calls": 0}

        def call(prompt: str) -> str:
            response = _chat(model, prompt, ref=ref, tenant_id=self.tenant_id, provider_factory=provider_factory)
            cost, usage = response_cost(model, response)
            totals["cost"] += cost
            totals["input"] += usage.input_tokens + usage.cache_read_tokens
            totals["output"] += usage.output_tokens
            totals["calls"] += 1
            return str(((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

        try:
            yield call
        finally:
            if totals["calls"]:
                self.charge(totals["cost"], purpose, ref, model=model, input_tokens=totals["input"],
                            output_tokens=totals["output"])

    def model_call(self, purpose: str, *, model: str, ref: str = "", estimate_usd: float = 0.0,
                   provider_factory: Callable[[str], Any] | None = None) -> Callable[[str], str]:
        """A ``prompt -> text`` callable (the shape of ``revision_guidance.build_model_call``) where
        every call checks the budget first and is charged at its actual cost."""

        def call(prompt: str) -> str:
            with self.track(purpose, ref=ref, model=model, estimate_usd=estimate_usd,
                            provider_factory=provider_factory) as inner:
                return inner(prompt)

        return call


def _chat(model: str, prompt: str, *, ref: str, tenant_id: str,
          provider_factory: Callable[[str], Any] | None) -> dict[str, Any]:
    if provider_factory is not None:
        provider = provider_factory(model)
    else:
        from ts_harness.agent.provider import build_provider

        provider = build_provider(model, scope={"tenant": tenant_id, "agent": USAGE_AGENT, "task": ref or None})
    response = provider.chat([{"role": "user", "content": prompt}], temperature=0)
    return response if isinstance(response, dict) else {}


def response_cost(model: str, response: dict[str, Any]) -> tuple[float, Any]:
    """(USD, Usage) of one response, priced with ``llm_pricing`` like the metering proxy."""
    from ts_harness.llm_proxy.metering import price, usage_from_body
    from ts_harness.llm_usage import pricing

    usage = usage_from_body(response)
    provider, _, name = model.partition("/") if "/" in model else ("", "", model)
    try:
        from ts_harness.agent.provider import resolve_provider

        endpoint = resolve_provider(model)
        provider, name = endpoint.provider, endpoint.model
    except Exception:  # noqa: BLE001 - price by the name as given
        pass
    return round(price(pricing(), provider, name, usage), 6), usage
