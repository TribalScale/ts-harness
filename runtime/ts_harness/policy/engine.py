"""Central policy enforcement with config-driven decisions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from ts_harness.config.schema import RuntimeConfig
from ts_harness.policy.permissions import (
    effective_bundles,
    holds_required_role,
    permissions_for_roles,
)
from ts_harness.state import get_state_store


Decision = Literal["allow", "deny", "approval_required"]


@dataclass
class PolicyResult:
    decision: Decision
    reason: str
    policy_id: str
    redaction_level: str = "none"


class PolicyEngine:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self._store = get_state_store()
        self._tenant_budgets: dict[str, float] = {}
        self._rate_windows: dict[tuple[str, str], tuple[int, float]] = {}

    def evaluate_rate_limit(self, *, tenant_id: str, tool_name: str, limit: int) -> PolicyResult:
        import time

        redis_key = f"ratelimit:{tenant_id}:{tool_name}"
        count = self._store.incr(redis_key, ttl_seconds=60)
        if count > limit:
            return PolicyResult("deny", "rate limit exceeded", "rate_limit")
        # Keep in-memory fallback for tests without Redis semantics
        key = (tenant_id, tool_name)
        now = time.time()
        mem_count, window_start = self._rate_windows.get(key, (0, now))
        if now - window_start >= 60:
            mem_count, window_start = 0, now
        mem_count += 1
        self._rate_windows[key] = (mem_count, window_start)
        if mem_count > limit:
            return PolicyResult("deny", "rate limit exceeded", "rate_limit")
        return PolicyResult("allow", "within rate limit", "rate_limit_ok")

    def evaluate_tool(self, tool_name: str, *, tenant_id: str, agent_id: str, source: str) -> PolicyResult:
        if source in {"slack", "discord", "telegram"} and tool_name in set(self.config.policy.channel_ingress_deny):
            return PolicyResult("deny", "channel ingress restriction", "channel_ingress")
        if tool_name in set(self.config.policy.approval_required_tools):
            return PolicyResult("approval_required", "approval required", "approval_required")
        return PolicyResult("allow", "allowed", "default")

    def evaluate_memory_access(self, *, tenant_id: str, action: str) -> PolicyResult:
        if not tenant_id:
            return PolicyResult("deny", "tenant required", "tenant_boundary")
        return PolicyResult("allow", "allowed", "memory_default")

    def evaluate_budget(self, *, tenant_id: str, cost_usd: float) -> PolicyResult:
        if os.environ.get("TS_HARNESS_REDIS_URL"):
            redis_key = f"budget:{tenant_id}:usd"
            prior = float(self._store.get(redis_key) or "0")
            total = prior + cost_usd
            self._store.set(redis_key, str(total))
        else:
            total = self._tenant_budgets.get(tenant_id, 0.0) + cost_usd
            self._tenant_budgets[tenant_id] = total
        if total >= self.config.policy.budget_hard_stop_usd:
            return PolicyResult("deny", "budget hard stop", "budget_hard_stop")
        if total >= self.config.policy.budget_warn_usd:
            return PolicyResult("allow", "budget warning threshold", "budget_warn")
        return PolicyResult("allow", "within budget", "budget_ok")

    def evaluate_identity(self, actor: dict[str, Any], required_roles: list[str]) -> PolicyResult:
        roles = [str(role) for role in (actor.get("roles") or [])]
        if not required_roles:
            return PolicyResult("allow", "identity ok", "rbac_ok")
        bundles = effective_bundles(self.config)
        granted = permissions_for_roles(roles, bundles)
        if any(holds_required_role(roles, granted, required, bundles) for required in required_roles):
            return PolicyResult("allow", "identity ok", "rbac_ok")
        return PolicyResult("deny", "missing required role", "rbac")

    def evaluate_permission(self, actor: dict[str, Any], permission: str) -> PolicyResult:
        from ts_harness.policy.permissions import allows

        roles = [str(role) for role in (actor.get("roles") or [])]
        granted = permissions_for_roles(roles, effective_bundles(self.config))
        if allows(granted, [permission]):
            return PolicyResult("allow", "permission ok", "permission_ok")
        return PolicyResult("deny", "missing permission", "permission")
