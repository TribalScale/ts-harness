"""Admin BFF for team LLM budgets.

Admins see and raise every team's budget. A team manager sees their teams and
may set a budget up to ``budgets.manager_max_monthly``; members see their teams
read-only. Every raise is audited as ``team_budget_raised``.
"""

from __future__ import annotations

from typing import Any

from ts_harness.gateway.auth import AuthContext


class BudgetAccessDenied(PermissionError):
    pass


def _is_admin(auth: AuthContext, config: Any) -> bool:
    if getattr(auth, "is_service", False):
        return True
    from ts_harness.policy.permissions import actor_permissions

    return "edit_config" in actor_permissions(auth, config)


def _team_roles(auth: AuthContext) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}
    for team in getattr(auth, "teams", None) or []:
        roles.setdefault(str(team.get("slug")), set()).add(str(team.get("role") or "member"))
    return roles


def _tenant(auth: AuthContext) -> str:
    return str(getattr(auth, "tenant_id", "") or "default")


def _manager_max(config: Any) -> float:
    budgets = getattr(config, "budgets", None)
    return float(getattr(budgets, "manager_max_monthly", 10000.0))


def list_budgets_admin(memory: Any, auth: AuthContext, config: Any) -> dict[str, Any]:
    data = memory.list_team_budgets(tenant_id=_tenant(auth))
    admin = _is_admin(auth, config)
    roles = _team_roles(auth)
    teams = []
    for team in data.get("teams") or []:
        team_roles = roles.get(team.get("slug"), set())
        if not admin and not team_roles:
            continue
        teams.append({**team, "can_raise": admin or "manager" in team_roles})
    payload = {**data, "teams": teams, "is_admin": admin, "raise_limit": None if admin else _manager_max(config)}
    if not admin:
        payload.pop("unassigned_spent_usd", None)
    return payload


def raise_budget_admin(memory: Any, auth: AuthContext, config: Any, team_id: str, body: dict[str, Any]) -> dict[str, Any]:
    teams = memory.list_team_budgets(tenant_id=_tenant(auth)).get("teams") or []
    team = next((t for t in teams if team_id in (t.get("id"), t.get("slug"))), None)
    if team is None:
        raise LookupError("team not found")
    admin = _is_admin(auth, config)
    if not admin and "manager" not in _team_roles(auth).get(team.get("slug"), set()):
        raise BudgetAccessDenied("only admins and the team's managers can change its budget")
    try:
        amount = float(body.get("amount_usd"))
    except (TypeError, ValueError) as exc:
        raise ValueError("amount_usd must be a number") from exc
    limit = _manager_max(config)
    if not admin and amount > limit:
        raise BudgetAccessDenied(f"managers can set a budget up to {limit:,.0f}; ask an admin for more")
    return memory.raise_team_budget(
        team["id"],
        tenant_id=_tenant(auth),
        amount_usd=amount,
        scope=body.get("scope") or "month",
        reason=body.get("reason"),
        actor=str(getattr(auth, "actor_id", "") or "admin"),
    )


def program_budget_admin(memory: Any, auth: AuthContext, config: Any, program_id: str) -> dict[str, Any]:
    """The owning team's budget status for the program banner."""
    data = memory.get_program_budget(program_id)
    team = data.get("team")
    if not team or data.get("tenant_id") != _tenant(auth):
        return {"team": None}
    fields = ("id", "slug", "display_name", "status", "percent", "held_tasks")
    return {"team": {k: team.get(k) for k in fields}}
