"""Model calls for improvement work, always through the improvement budget.

:func:`metered_call` is a ``prompt -> text`` callable like ``ImprovementBudget.model_call`` that
also adds up what it spent (``call.spent``), so a proposal can record the cost of its digest or
evaluation. Every call checks the budget first and raises :class:`BudgetExhausted` when the month's
cap is reached; callers stop quietly.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ts_harness.improvement.budget import USAGE_AGENT, BudgetExhausted, ImprovementBudget, response_cost

__all__ = ["BudgetExhausted", "MeteredCall", "improvement_model", "metered_call"]


def improvement_model() -> str | None:
    """``TS_HARNESS_IMPROVEMENT_MODEL``, else the revision diagnosis model; None when neither is set."""
    configured = os.environ.get("TS_HARNESS_IMPROVEMENT_MODEL", "").strip()
    if configured.lower() in {"off", "none", "0", "false"}:
        return None
    if configured:
        return configured
    try:
        from ts_harness.workflows.revision_guidance import diagnosis_model

        return diagnosis_model() or None
    except Exception:  # noqa: BLE001
        return None


class MeteredCall:
    """``call(prompt) -> text``; ``call.spent`` is the USD charged so far, ``call.calls`` the count."""

    def __init__(self, budget: ImprovementBudget, purpose: str, *, model: str, ref: str = "",
                 estimate_usd: float = 0.0, provider_factory: Callable[[str], Any] | None = None):
        self.budget, self.purpose, self.model, self.ref = budget, purpose, model, ref
        self.estimate_usd = estimate_usd
        self.spent = 0.0
        self.calls = 0
        self._factory = provider_factory

    def _provider(self, model: str) -> Any:
        if self._factory is not None:
            inner = self._factory(model)
        else:
            from ts_harness.agent.provider import build_provider

            inner = build_provider(model, scope={"tenant": self.budget.tenant_id, "agent": USAGE_AGENT,
                                                 "task": self.ref or None})
        outer = self

        class Recording:
            def chat(self, messages: Any, **kwargs: Any) -> Any:
                response = inner.chat(messages, **kwargs)
                try:
                    outer.spent += response_cost(model, response if isinstance(response, dict) else {})[0]
                except Exception:  # noqa: BLE001 - the budget still charges it
                    pass
                outer.calls += 1
                return response

        return Recording()

    def __call__(self, prompt: str) -> str:
        with self.budget.track(self.purpose, ref=self.ref, model=self.model, estimate_usd=self.estimate_usd,
                               provider_factory=self._provider) as call:
            return call(prompt)


def metered_call(purpose: str, *, ref: str = "", model: str | None = None, budget: ImprovementBudget | None = None,
                 estimate_usd: float = 0.0, tenant_id: str = "default") -> MeteredCall | None:
    """A metered model call for ``purpose``, or None when no improvement model is configured."""
    chosen = model or improvement_model()
    if not chosen:
        return None
    return MeteredCall(budget or ImprovementBudget(tenant_id=tenant_id), purpose, model=chosen, ref=ref,
                       estimate_usd=estimate_usd)
