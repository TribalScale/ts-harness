"""Team LLM budgets: settings from the runtime config, spend from ``llm_usage``, raises and alerts.

The effective budget and the spend come from the SQL functions in migration
041 (``team_monthly_budget``, ``team_month_spend``) so the claim function, the
LLM proxy and these routes agree on the numbers.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

SCOPES = ("month", "permanent")
HELD_REASON = "team budget reached"


def _tenant(body: dict[str, Any]) -> str:
    return str(body.get("tenant_id") or "default")


def _amount(value: Any, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} must be zero or more")
    return amount.quantize(Decimal("0.01"))


def _status(spent: float, budget: float, warn_at: int) -> str:
    if spent >= budget:
        return "reached"
    if budget > 0 and spent * 100 >= budget * warn_at:
        return "warning"
    return "ok"


def put_budget_settings(conn, body: dict[str, Any]) -> dict[str, Any]:
    tenant = _tenant(body)
    teams = body.get("teams") if isinstance(body.get("teams"), dict) else {}
    amounts = {str(key): str(_amount(value, f"teams.{key}")) for key, value in teams.items()}
    warn = int(body.get("warn_at_percent") or 80)
    if not 1 <= warn <= 100:
        raise ValueError("warn_at_percent must be between 1 and 100")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO budget_settings (tenant_id, currency, default_team_monthly, warn_at_percent,
                                         manager_max_monthly, team_amounts, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
            ON CONFLICT (tenant_id) DO UPDATE
               SET currency = EXCLUDED.currency,
                   default_team_monthly = EXCLUDED.default_team_monthly,
                   warn_at_percent = EXCLUDED.warn_at_percent,
                   manager_max_monthly = EXCLUDED.manager_max_monthly,
                   team_amounts = EXCLUDED.team_amounts,
                   updated_at = now()
            RETURNING *
            """,
            (
                tenant,
                str(body.get("currency") or "USD"),
                _amount(body.get("default_team_monthly", 5000), "default_team_monthly"),
                warn,
                _amount(body.get("manager_max_monthly", 10000), "manager_max_monthly"),
                json.dumps(amounts),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return _settings(row, tenant)


def _settings(row: dict[str, Any] | None, tenant: str) -> dict[str, Any]:
    row = row or {}
    return {
        "tenant_id": tenant,
        "currency": row.get("currency") or "USD",
        "default_team_monthly": float(row.get("default_team_monthly") or 5000),
        "warn_at_percent": int(row.get("warn_at_percent") or 80),
        "manager_max_monthly": float(row.get("manager_max_monthly") or 10000),
    }


def list_team_budgets(conn, params: dict[str, Any]) -> dict[str, Any]:
    """This month's budget, spend and status per team, with recent raises."""
    tenant = _tenant(params)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM budget_settings WHERE tenant_id = %s", (tenant,))
        settings = _settings(cur.fetchone(), tenant)
        cur.execute("SELECT date_trunc('month', now())::date AS month")
        month = cur.fetchone()["month"]
        cur.execute(
            """
            SELECT tm.id::text AS id, tm.slug, tm.display_name,
                   team_monthly_budget(tm.tenant_id, tm.id::text, %(month)s) AS budget_usd,
                   team_month_spend(tm.tenant_id, tm.id::text, %(month)s) AS spent_usd,
                   (SELECT count(*) FROM workflow_tasks t
                     WHERE t.team_id = tm.id AND t.status = 'pending' AND t.held_reason = %(held)s) AS held_tasks
              FROM teams tm
             WHERE tm.tenant_id = %(tenant)s
             ORDER BY tm.display_name, tm.slug
            """,
            {"tenant": tenant, "month": month, "held": HELD_REASON},
        )
        teams = list(cur.fetchall())
        cur.execute(
            """
            SELECT id::text AS id, team_id, month, amount_usd, raised_by, reason, created_at
              FROM team_budget_overrides
             WHERE tenant_id = %s AND (month IS NULL OR month = %s)
             ORDER BY created_at DESC
            """,
            (tenant, month),
        )
        overrides: dict[str, list[dict[str, Any]]] = {}
        for row in cur.fetchall():
            overrides.setdefault(row["team_id"], []).append(
                {**row, "amount_usd": float(row["amount_usd"]), "scope": "permanent" if row["month"] is None else "month"}
            )
        cur.execute(
            "SELECT COALESCE(sum(cost_usd), 0) AS spent FROM llm_usage "
            "WHERE tenant_id = %s AND team_id IS NULL AND ts >= %s AND ts < %s::date + interval '1 month'",
            (tenant, month, month),
        )
        unassigned = float(cur.fetchone()["spent"])
    rows = []
    for team in teams:
        budget, spent = float(team["budget_usd"]), float(team["spent_usd"])
        rows.append(
            {
                **team,
                "budget_usd": budget,
                "spent_usd": round(spent, 2),
                "percent": round(spent * 100 / budget, 1) if budget else (100.0 if spent else 0.0),
                "status": _status(spent, budget, settings["warn_at_percent"]),
                "held_tasks": int(team["held_tasks"]),
                "overrides": overrides.get(team["id"], [])[:5],
            }
        )
    return {**settings, "month": month, "teams": rows, "unassigned_spent_usd": round(unassigned, 2)}


def team_budget(conn, team_id: str, params: dict[str, Any]) -> dict[str, Any]:
    for team in list_team_budgets(conn, params)["teams"]:
        if team["id"] == team_id or team["slug"] == team_id:
            return team
    raise LookupError("team not found")


def raise_team_budget(conn, team_id: str, body: dict[str, Any]) -> dict[str, Any]:
    tenant = _tenant(body)
    amount = _amount(body.get("amount_usd"), "amount_usd")
    scope = str(body.get("scope") or "month")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ValueError("reason is required")
    actor = str(body.get("actor") or "").strip() or "admin"
    with conn.cursor() as cur:
        try:
            team_uuid = str(uuid.UUID(team_id))
        except ValueError:
            team_uuid = None
        cur.execute(
            "SELECT id::text AS id, slug FROM teams WHERE tenant_id = %s AND (id::text = %s OR slug = %s)",
            (tenant, team_uuid, team_id),
        )
        team = cur.fetchone()
        if team is None:
            raise LookupError("team not found")
        cur.execute(
            """
            INSERT INTO team_budget_overrides (tenant_id, team_id, month, amount_usd, raised_by, reason)
            VALUES (%s, %s, CASE WHEN %s = 'month' THEN date_trunc('month', now())::date END, %s, %s, %s)
            RETURNING id::text AS id, month
            """,
            (tenant, team["id"], scope, amount, actor, reason),
        )
        override = cur.fetchone()
        # Alerts above the new budget's status are cleared so they fire again if it is crossed.
        cur.execute(
            """
            SELECT team_month_spend(%(t)s, %(team)s, m) AS spent, team_monthly_budget(%(t)s, %(team)s, m) AS budget,
                   COALESCE((SELECT warn_at_percent FROM budget_settings WHERE tenant_id = %(t)s), 80) AS warn
              FROM (SELECT date_trunc('month', now())::date AS m) month
            """,
            {"t": tenant, "team": team["id"]},
        )
        now = cur.fetchone()
        keep = {"reached": ["warning", "reached"], "warning": ["warning"]}.get(
            _status(float(now["spent"]), float(now["budget"]), int(now["warn"])), []
        )
        cur.execute(
            "DELETE FROM team_budget_alerts WHERE tenant_id = %s AND team_id = %s "
            "AND month = date_trunc('month', now())::date AND NOT (level = ANY(%s))",
            (tenant, team["id"], keep),
        )
        details = {"team_id": team["id"], "team_slug": team["slug"], "amount_usd": str(amount), "scope": scope,
                   "month": override["month"], "reason": reason}
        cur.execute(
            """
            INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, tenant_id, occurred_at)
            VALUES (uuid_generate_v4(), %s, %s, 'team_budget_raised', %s, %s::jsonb, %s, now())
            """,
            (f"team:{team['id']}", actor, f"Budget for {team['slug']} set to {amount} USD ({scope})",
             json.dumps(details, default=str), tenant),
        )
    conn.commit()
    return {"override_id": override["id"], **team_budget(conn, team["id"], {"tenant_id": tenant})}


def claim_budget_alerts(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Teams that crossed the warning or the full budget this month and were not told yet; marks them told."""
    tenant = _tenant(body)
    summary = list_team_budgets(conn, {"tenant_id": tenant})
    due: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for team in summary["teams"]:
            levels = {"reached": ["warning", "reached"], "warning": ["warning"]}.get(team["status"], [])
            for level in levels:
                cur.execute(
                    """
                    INSERT INTO team_budget_alerts (tenant_id, team_id, month, level, percent)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING level
                    """,
                    (tenant, team["id"], summary["month"], level, team["percent"]),
                )
                if cur.fetchone():
                    due.append({"level": level, **{k: team[k] for k in ("id", "slug", "display_name", "budget_usd",
                                                                          "spent_usd", "percent", "held_tasks")}})
    conn.commit()
    # Only the highest new level per team needs a message.
    highest: dict[str, dict[str, Any]] = {}
    for alert in due:
        if alert["id"] not in highest or alert["level"] == "reached":
            highest[alert["id"]] = alert
    return {"month": summary["month"], "alerts": list(highest.values())}


def program_team_budget(conn, program_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Budget status of the team that owns the program's project, or ``{"team": None}``."""
    try:
        program_uuid = str(uuid.UUID(program_id))
    except ValueError as exc:
        raise LookupError("program not found") from exc
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.tenant_id, pr.team_id::text AS team_id
              FROM delivery_programs p
              LEFT JOIN projects pr ON pr.tenant_id = p.tenant_id AND pr.key = p.project_id
             WHERE p.id = %s
            """,
            (program_uuid,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError("program not found")
    if not row["team_id"]:
        return {"tenant_id": row["tenant_id"], "team": None}
    return {"tenant_id": row["tenant_id"], "team": team_budget(conn, row["team_id"], {"tenant_id": row["tenant_id"]})}
