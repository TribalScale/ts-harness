"""Scale-out metrics snapshot: lane queues, coding jobs, gate waits, GitHub rate limits and LLM spend.

One query set for the publish script and the runtime's Prometheus gauges.
Window counts cover the last ``window_seconds`` (default 120, the publish
interval) so each publish reports what happened since the previous one.
"""

from __future__ import annotations

from typing import Any

LANES = ("fast", "agent", "coding")
DEFAULT_WINDOW_SECONDS = 120
FINISHED_JOB_STATUSES = ("succeeded", "failed", "lost", "cancelled")

_READY_DEPS = """
    NOT EXISTS (
        SELECT 1 FROM workflow_tasks dep
         WHERE dep.id = ANY(t.depends_on) AND dep.status <> 'succeeded'
    )
"""

_WAITING_SINCE = """
    GREATEST(
        t.created_at,
        COALESCE(t.next_attempt_at, t.created_at),
        COALESCE((SELECT max(dep.completed_at) FROM workflow_tasks dep WHERE dep.id = ANY(t.depends_on)), t.created_at)
    )
"""


def _window(params: dict[str, Any]) -> int:
    try:
        value = int(params.get("window_seconds") or DEFAULT_WINDOW_SECONDS)
    except (TypeError, ValueError) as exc:
        raise ValueError("window_seconds must be a whole number") from exc
    if not 10 <= value <= 86400:
        raise ValueError("window_seconds must be between 10 and 86400")
    return value


def _num(value: Any) -> float:
    return round(float(value or 0), 6)


def _lanes(cur) -> list[dict[str, Any]]:
    cur.execute(
        f"""
        SELECT t.lane,
               count(*) FILTER (WHERE t.held_reason IS NULL) AS ready,
               count(*) FILTER (WHERE t.held_reason IS NOT NULL) AS held,
               COALESCE(EXTRACT(EPOCH FROM now() - min({_WAITING_SINCE}) FILTER (WHERE t.held_reason IS NULL)), 0) AS oldest_seconds
          FROM workflow_tasks t
         WHERE t.status = 'pending'
           AND t.attempt <= t.max_retries
           AND (t.approval_required = false OR t.approval_status = 'approved')
           AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= now())
           AND {_READY_DEPS}
         GROUP BY t.lane
        """
    )
    found = {r["lane"]: r for r in cur.fetchall()}
    cur.execute("SELECT lane, count(*) AS running FROM workflow_tasks WHERE status = 'running' GROUP BY lane")
    running = {r["lane"]: int(r["running"]) for r in cur.fetchall()}
    return [
        {
            "lane": lane,
            "ready": int((found.get(lane) or {}).get("ready") or 0),
            "held": int((found.get(lane) or {}).get("held") or 0),
            "running": running.get(lane, 0),
            "oldest_seconds": _num((found.get(lane) or {}).get("oldest_seconds")),
        }
        for lane in LANES
    ]


_JOB_TEAM = "COALESCE(tm.slug, j.team_slug, 'none')"
_JOB_FROM = """
      FROM coding_jobs j
      LEFT JOIN workflow_tasks t ON t.id = j.task_id
      LEFT JOIN teams tm ON tm.id = t.team_id
"""


def _coding_jobs(cur, window: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'queued') AS queued,
               COALESCE(EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status = 'queued')), 0) AS oldest_queued_seconds,
               count(*) FILTER (WHERE status IN ('starting', 'running')) AS running
          FROM coding_jobs
        """
    )
    totals = cur.fetchone() or {}
    cur.execute(
        f"""
        SELECT {_JOB_TEAM} AS team, count(*) AS running
        {_JOB_FROM}
         WHERE j.status IN ('starting', 'running')
         GROUP BY 1 ORDER BY 1
        """
    )
    by_team = [{"team": r["team"], "running": int(r["running"])} for r in cur.fetchall()]
    cur.execute(
        """
        SELECT status,
               count(*) AS jobs,
               avg(EXTRACT(EPOCH FROM finished_at - COALESCE(started_at, claimed_at, created_at))) AS avg_seconds,
               max(EXTRACT(EPOCH FROM finished_at - COALESCE(started_at, claimed_at, created_at))) AS max_seconds
          FROM coding_jobs
         WHERE finished_at >= now() - make_interval(secs => %s)
           AND status = ANY(%s)
         GROUP BY status
        """,
        (window, list(FINISHED_JOB_STATUSES)),
    )
    finished = {r["status"]: r for r in cur.fetchall()}
    outcomes = {status: int((finished.get(status) or {}).get("jobs") or 0) for status in FINISHED_JOB_STATUSES}
    done = sum(outcomes.values())
    completed = [r for s, r in finished.items() if s in ("succeeded", "failed")]
    completed_jobs = sum(int(r["jobs"]) for r in completed)
    avg = sum(float(r["avg_seconds"] or 0) * int(r["jobs"]) for r in completed) / completed_jobs if completed_jobs else 0
    return {
        "queued": int(totals.get("queued") or 0),
        "oldest_queued_seconds": _num(totals.get("oldest_queued_seconds")),
        "running": int(totals.get("running") or 0),
        "running_by_team": by_team,
        "finished": outcomes,
        "lost_rate": round(outcomes["lost"] / done, 4) if done else 0.0,
        "avg_duration_seconds": round(avg, 1),
        "max_duration_seconds": round(max((float(r["max_seconds"] or 0) for r in completed), default=0), 1),
    }


def _gates(cur, window: int) -> list[dict[str, Any]]:
    cur.execute(
        f"""
        SELECT t.step_name AS gate,
               count(*) AS waiting,
               EXTRACT(EPOCH FROM now() - min({_WAITING_SINCE})) AS oldest_seconds
          FROM workflow_tasks t
         WHERE t.status = 'pending'
           AND t.approval_required = true
           AND COALESCE(t.approval_status, 'pending') = 'pending'
           AND {_READY_DEPS}
         GROUP BY t.step_name
        """
    )
    gates: dict[str, dict[str, Any]] = {
        r["gate"]: {"gate": r["gate"], "waiting": int(r["waiting"]), "oldest_seconds": _num(r["oldest_seconds"]),
                    "decided": 0, "avg_wait_seconds": 0.0}
        for r in cur.fetchall()
    }
    cur.execute(
        f"""
        SELECT t.step_name AS gate,
               count(*) AS decided,
               avg(GREATEST(EXTRACT(EPOCH FROM d.created_at - {_WAITING_SINCE}), 0)) AS avg_wait_seconds
          FROM workflow_decisions d
          JOIN workflow_tasks t ON t.id = d.task_id
         WHERE d.kind = 'gate'
           AND d.created_at >= now() - make_interval(secs => %s)
         GROUP BY t.step_name
        """,
        (window,),
    )
    for r in cur.fetchall():
        entry = gates.setdefault(r["gate"], {"gate": r["gate"], "waiting": 0, "oldest_seconds": 0.0})
        entry["decided"] = int(r["decided"])
        entry["avg_wait_seconds"] = round(float(r["avg_wait_seconds"] or 0), 1)
    return sorted(gates.values(), key=lambda g: g["gate"])


def _github(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT host, installation_id, account_login, rate_limit_remaining, rate_limit_limit,
               EXTRACT(EPOCH FROM now() - rate_limit_observed_at) AS age_seconds
          FROM github_installations
         WHERE rate_limit_limit > 0 AND suspended_at IS NULL
         ORDER BY host, installation_id
        """
    )
    return [
        {
            "host": r["host"],
            "installation_id": int(r["installation_id"]),
            "account": r["account_login"],
            "remaining": int(r["rate_limit_remaining"]),
            "limit": int(r["rate_limit_limit"]),
            "remaining_percent": round(100.0 * int(r["rate_limit_remaining"]) / int(r["rate_limit_limit"]), 1),
            "age_seconds": _num(r["age_seconds"]),
        }
        for r in cur.fetchall()
    ]


def _llm(cur, window: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT COALESCE(tm.slug, u.team_id, 'none') AS team,
               sum(u.input_tokens) FILTER (WHERE u.ts >= now() - make_interval(secs => %s)) AS window_input_tokens,
               sum(u.output_tokens) FILTER (WHERE u.ts >= now() - make_interval(secs => %s)) AS window_output_tokens,
               sum(u.cost_usd) FILTER (WHERE u.ts >= now() - make_interval(secs => %s)) AS window_cost_usd,
               sum(u.cost_usd) AS month_cost_usd
          FROM llm_usage u
          LEFT JOIN teams tm ON tm.id::text = u.team_id
         WHERE u.ts >= date_trunc('month', now())
         GROUP BY 1 ORDER BY 1
        """,
        (window, window, window),
    )
    return [
        {
            "team": r["team"],
            "window_input_tokens": int(r["window_input_tokens"] or 0),
            "window_output_tokens": int(r["window_output_tokens"] or 0),
            "window_cost_usd": _num(r["window_cost_usd"]),
            "month_cost_usd": _num(r["month_cost_usd"]),
        }
        for r in cur.fetchall()
    ]


def snapshot(conn, params: dict[str, Any]) -> dict[str, Any]:
    window = _window(params)
    with conn.cursor() as cur:
        return {
            "window_seconds": window,
            "lanes": _lanes(cur),
            "coding_jobs": _coding_jobs(cur, window),
            "gates": _gates(cur, window),
            "github": _github(cur),
            "llm": _llm(cur, window),
        }
