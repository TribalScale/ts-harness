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
PROGRAM_ACTIONS = ("downgrade", "stop")
PROGRAM_HELD_REASON = "program budget reached"


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
    action = str(body.get("program_action") or "downgrade").strip().lower()
    if action not in PROGRAM_ACTIONS:
        raise ValueError(f"program_action must be one of {', '.join(PROGRAM_ACTIONS)}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO budget_settings (tenant_id, currency, default_team_monthly, warn_at_percent,
                                         manager_max_monthly, team_amounts, program_default_usd,
                                         program_action, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, now())
            ON CONFLICT (tenant_id) DO UPDATE
               SET currency = EXCLUDED.currency,
                   default_team_monthly = EXCLUDED.default_team_monthly,
                   warn_at_percent = EXCLUDED.warn_at_percent,
                   manager_max_monthly = EXCLUDED.manager_max_monthly,
                   team_amounts = EXCLUDED.team_amounts,
                   program_default_usd = EXCLUDED.program_default_usd,
                   program_action = EXCLUDED.program_action,
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
                _amount(body.get("program_default_usd") or 0, "program_default_usd"),
                action,
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
        "program_default_usd": float(row.get("program_default_usd") or 0),
        "program_action": row.get("program_action") or "downgrade",
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
    team = team_budget(conn, row["team_id"], {"tenant_id": row["tenant_id"]}) if row["team_id"] else None
    return {"tenant_id": row["tenant_id"], "team": team, "program": program_budget(conn, program_uuid)}


# Program budgets (migration 051): a total per delivery program, from ``program_budget``
# and ``program_spend``. 0 means the program has no budget.

_PROGRAM_SQL = """
    SELECT p.id::text AS id, p.tenant_id, COALESCE(NULLIF(p.display_name, ''), p.summary) AS name,
           p.status AS program_status, p.source_channel, p.budget_usd AS own_budget,
           p.budget_set_by, p.budget_set_at,
           program_budget(p.id) AS budget_usd, program_spend(p.id) AS spent_usd,
           (SELECT COALESCE(sum(u.cost_usd), 0) FROM llm_usage u
             WHERE u.program_id = p.id::text AND u.ts >= date_trunc('month', now())) AS spent_month_usd,
           COALESCE(s.warn_at_percent, 80) AS warn_at_percent,
           COALESCE(s.program_action, 'downgrade') AS action,
           COALESCE(s.currency, 'USD') AS currency,
           (SELECT count(*) FROM coding_jobs j
             WHERE j.status = 'queued' AND j.held_reason = %(held)s
               AND COALESCE(j.program_id, workflow_program_id(j.workflow_id)) = p.id) AS held_jobs
      FROM delivery_programs p
      LEFT JOIN budget_settings s ON s.tenant_id = p.tenant_id
"""


def _program_uuid(program_id: str) -> str:
    try:
        return str(uuid.UUID(str(program_id)))
    except ValueError as exc:
        raise LookupError("program not found") from exc


def _program_status(row: dict[str, Any]) -> dict[str, Any]:
    budget, spent = float(row["budget_usd"] or 0), float(row["spent_usd"] or 0)
    own = row.get("own_budget")
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "name": row["name"],
        "budget_usd": budget,
        "source": "program" if own is not None else ("default" if budget > 0 else "none"),
        "spent_usd": round(spent, 2),
        "spent_month_usd": round(float(row["spent_month_usd"] or 0), 2),
        "percent": round(spent * 100 / budget, 1) if budget > 0 else None,
        "status": _status(spent, budget, int(row["warn_at_percent"])) if budget > 0 else "none",
        "action": row["action"],
        "currency": row["currency"],
        "warn_at_percent": int(row["warn_at_percent"]),
        "held_jobs": int(row["held_jobs"] or 0),
        "set_by": row.get("budget_set_by") if own is not None else None,
        "set_at": row.get("budget_set_at") if own is not None else None,
    }


def program_budget(conn, program_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The program's budget, total and this month's spend, status and the action at 100%."""
    with conn.cursor() as cur:
        cur.execute(_PROGRAM_SQL + " WHERE p.id = %(id)s", {"id": _program_uuid(program_id), "held": PROGRAM_HELD_REASON})
        row = cur.fetchone()
    if row is None:
        raise LookupError("program not found")
    return _program_status(row)


def set_program_budget(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Set the program's own budget (``amount_usd``), or go back to the default with ``amount_usd: null``."""
    program_uuid = _program_uuid(program_id)
    raw = body.get("amount_usd")
    amount = None if raw in (None, "") else _amount(raw, "amount_usd")
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ValueError("reason is required")
    actor = str(body.get("actor") or "").strip() or "admin"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE delivery_programs
               SET budget_usd = %s, budget_set_by = %s, budget_set_at = now(), updated_at = now()
             WHERE id = %s AND tenant_id = %s
            RETURNING id::text AS id, tenant_id
            """,
            (amount, actor, program_uuid, _tenant(body)),
        )
        program = cur.fetchone()
        if program is None:
            raise LookupError("program not found")
        cur.execute(_PROGRAM_SQL + " WHERE p.id = %(id)s", {"id": program_uuid, "held": PROGRAM_HELD_REASON})
        now = _program_status(cur.fetchone())
        # Alerts above the new status are cleared so they are sent again if it is crossed.
        keep = {"reached": ["warning", "reached"], "warning": ["warning"]}.get(now["status"], [])
        cur.execute("DELETE FROM program_budget_alerts WHERE program_id = %s AND NOT (level = ANY(%s))", (program_uuid, keep))
        label = f"{amount} {now['currency']}" if amount is not None else "the default"
        details = {"program_id": program_uuid, "amount_usd": None if amount is None else str(amount), "reason": reason}
        cur.execute(
            """
            INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, tenant_id, occurred_at)
            VALUES (uuid_generate_v4(), %s, %s, 'program_budget_set', %s, %s::jsonb, %s, now())
            """,
            (f"program:{program_uuid}", actor, f"Budget for program {now['name']} set to {label}",
             json.dumps(details), program["tenant_id"]),
        )
    conn.commit()
    return now


def claim_program_budget_alerts(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Running programs that crossed the warning or their budget and were not told yet, plus ticket notes.

    ``notes`` are coding jobs whose ticket thread must hear why the job waits or ran on the default
    model (``coding_jobs.budget_note``). Both are marked as told here.
    """
    tenant = _tenant(body)
    due: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            _PROGRAM_SQL + " WHERE p.tenant_id = %(tenant)s AND p.status IN ('running', 'blocked') AND program_budget(p.id) > 0",
            {"tenant": tenant, "held": PROGRAM_HELD_REASON},
        )
        for row in cur.fetchall():
            status = _program_status(row)
            levels = {"reached": ["warning", "reached"], "warning": ["warning"]}.get(status["status"], [])
            sent: list[str] = []
            for level in levels:
                cur.execute(
                    """
                    INSERT INTO program_budget_alerts (program_id, level, percent, action)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING level
                    """,
                    (status["id"], level, status["percent"], status["action"] if level == "reached" else None),
                )
                if cur.fetchone():
                    sent.append(level)
            if sent:
                level = "reached" if "reached" in sent else "warning"
                due.append({"level": level, "source_channel": row.get("source_channel"), **status})
        cur.execute(
            """
            UPDATE coding_jobs
               SET budget_noted_at = now()
             WHERE tenant_id = %s AND budget_note IS NOT NULL AND budget_noted_at IS NULL
            RETURNING id::text AS id, workflow_id::text AS workflow_id, work_order_id, budget_note
            """,
            (tenant,),
        )
        notes = list(cur.fetchall())
    conn.commit()
    return {"alerts": due, "notes": notes}


# Improvement budget (migration 055): the self-improvement loop's model spend per calendar month
# (UTC), apart from delivery budgets. The cap is a runtime setting; these routes only keep and sum
# the ledger (see ts_harness.improvement.budget).

MAX_CHARGE_USD = Decimal("1000")


def _month(value: Any) -> str | None:
    """``YYYY-MM`` or ``YYYY-MM-DD`` to the month's first day; ``None`` for the current month."""
    import re

    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", text)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ValueError("month must be YYYY-MM")
    return f"{match.group(1)}-{match.group(2)}-01"


def improvement_spend(conn, params: dict[str, Any]) -> dict[str, Any]:
    """The month's improvement spend: total, by purpose and the latest charges."""
    tenant = _tenant(params)
    month = _month(params.get("month"))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(%s::date, (date_trunc('month', now() AT TIME ZONE 'UTC'))::date) AS month",
            (month,),
        )
        month_day = cur.fetchone()["month"]
        cur.execute(
            "SELECT purpose, COALESCE(sum(cost_usd), 0) AS spent, count(*) AS charges "
            "FROM improvement_spend WHERE tenant_id = %s AND month = %s GROUP BY purpose ORDER BY purpose",
            (tenant, month_day),
        )
        by_purpose = {row["purpose"]: {"spent_usd": round(float(row["spent"]), 4), "charges": int(row["charges"])}
                      for row in cur.fetchall()}
        cur.execute(
            "SELECT id::text AS id, cost_usd, purpose, ref, model, input_tokens, output_tokens, actor, created_at "
            "FROM improvement_spend WHERE tenant_id = %s AND month = %s ORDER BY created_at DESC LIMIT 20",
            (tenant, month_day),
        )
        recent = [{**row, "cost_usd": round(float(row["cost_usd"]), 4)} for row in cur.fetchall()]
    spent = round(sum(item["spent_usd"] for item in by_purpose.values()), 4)
    return {"tenant_id": tenant, "month": str(month_day), "spent_usd": spent, "by_purpose": by_purpose, "recent": recent}


def _tokens(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a whole number") from exc
    if number < 0:
        raise ValueError(f"{key} must be zero or more")
    return number


def charge_improvement(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Record one improvement charge; returns the month's spend after it."""
    tenant = _tenant(body)
    amount = _amount(body.get("cost_usd"), "cost_usd") if body.get("cost_usd") not in (None, "") else None
    if amount is None:
        raise ValueError("cost_usd is required")
    exact = Decimal(str(body.get("cost_usd"))).quantize(Decimal("0.0001"))
    if exact > MAX_CHARGE_USD:
        raise ValueError(f"cost_usd must be at most {MAX_CHARGE_USD}")
    purpose = str(body.get("purpose") or "").strip()[:80]
    if not purpose:
        raise ValueError("purpose is required")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO improvement_spend (tenant_id, cost_usd, purpose, ref, model, input_tokens, output_tokens, actor)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id::text AS id, month
            """,
            (tenant, exact, purpose, str(body.get("ref") or "")[:200] or None,
             str(body.get("model") or "")[:200] or None, _tokens(body, "input_tokens"), _tokens(body, "output_tokens"),
             str(body.get("actor") or "")[:120] or None),
        )
        row = cur.fetchone()
    conn.commit()
    return {"charge_id": row["id"], **improvement_spend(conn, {"tenant_id": tenant, "month": str(row["month"])})}
