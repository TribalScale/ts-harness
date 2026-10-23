"""Scale-out metrics: GitHub installation rate limits and Prometheus gauges.

The numbers come from the memory API snapshot (``GET /metrics/scale-out``);
``scripts/ops/scale_metrics_payload.py`` maps the same snapshot to CloudWatch.
GitHub rate limits are refreshed by the ``github_rate_limits`` system job,
which asks GitHub for each installation's remaining quota and stores it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

SNAPSHOT_CACHE_SECONDS = 30
_cache: dict[str, Any] = {"at": 0.0, "snapshot": None}
_cache_lock = threading.Lock()


def refresh_github_rate_limits(
    memory: Any = None, *, app_lookup: Callable[[str], Any] | None = None
) -> dict[str, Any]:
    """Read the core rate limit of every active installation and store it in Postgres."""
    from ts_harness.integrations.github_app import app_for

    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    lookup = app_lookup or app_for
    try:
        installations = [i for i in memory.list_github_installations() if not i.get("suspended_at")]
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"could not list installations: {exc}"}
    limits: list[dict[str, Any]] = []
    errors: list[str] = []
    apps: dict[str, Any] = {}
    for item in installations:
        host = str(item.get("host") or "github.com")
        if host not in apps:
            apps[host] = lookup(host)
        app = apps[host]
        if app is None:
            continue
        installation_id = int(item["installation_id"])
        try:
            limit = app.installation_rate_limit(installation_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host}/{installation_id}: {exc}")
            continue
        limits.append({"host": host, "installation_id": installation_id, **limit})
    if limits:
        memory.record_github_rate_limits(limits)
    _set_installation_gauges(limits)
    result: dict[str, Any] = {"status": "ok" if not errors else "partial", "installations": len(limits)}
    if errors:
        result["errors"] = errors[:10]
    return result


def _set_installation_gauges(limits: list[dict[str, Any]]) -> None:
    from ts_harness.observability import prometheus

    if not prometheus.PROMETHEUS_AVAILABLE:
        return
    for item in limits:
        prometheus.github_installation_rate_limit_remaining.labels(
            host=item["host"], installation=str(item["installation_id"])
        ).set(item["remaining"])


def observe_snapshot(snapshot: dict[str, Any]) -> None:
    """Set the Prometheus gauges from a snapshot, dropping label sets that are gone."""
    from ts_harness.observability import prometheus as p

    if not p.PROMETHEUS_AVAILABLE:
        return
    for gauge in (p.lane_ready_tasks, p.lane_held_tasks, p.lane_oldest_ready_seconds, p.coding_jobs_running,
                  p.coding_jobs_finished, p.gate_waiting, p.gate_oldest_wait_seconds, p.gate_avg_wait_seconds,
                  p.llm_tokens, p.llm_month_cost_usd, p.github_installation_rate_limit_remaining):
        gauge.clear()
    for lane in snapshot.get("lanes") or []:
        p.lane_ready_tasks.labels(lane=lane["lane"]).set(lane["ready"])
        p.lane_held_tasks.labels(lane=lane["lane"]).set(lane["held"])
        p.lane_oldest_ready_seconds.labels(lane=lane["lane"]).set(lane["oldest_seconds"])
    jobs = snapshot.get("coding_jobs") or {}
    p.coding_jobs_queued.set(jobs.get("queued") or 0)
    p.coding_job_oldest_queued_seconds.set(jobs.get("oldest_queued_seconds") or 0)
    p.coding_job_avg_seconds.set(jobs.get("avg_duration_seconds") or 0)
    for row in jobs.get("running_by_team") or []:
        p.coding_jobs_running.labels(team=row["team"]).set(row["running"])
    for outcome, count in (jobs.get("finished") or {}).items():
        p.coding_jobs_finished.labels(outcome=outcome).set(count)
    for gate in snapshot.get("gates") or []:
        p.gate_waiting.labels(gate=gate["gate"]).set(gate["waiting"])
        p.gate_oldest_wait_seconds.labels(gate=gate["gate"]).set(gate["oldest_seconds"])
        p.gate_avg_wait_seconds.labels(gate=gate["gate"]).set(gate.get("avg_wait_seconds") or 0)
    for row in snapshot.get("llm") or []:
        p.llm_tokens.labels(team=row["team"], direction="input").set(row["window_input_tokens"])
        p.llm_tokens.labels(team=row["team"], direction="output").set(row["window_output_tokens"])
        p.llm_month_cost_usd.labels(team=row["team"]).set(row["month_cost_usd"])
    for item in snapshot.get("github") or []:
        p.github_installation_rate_limit_remaining.labels(
            host=item["host"], installation=str(item["installation_id"])
        ).set(item["remaining"])


def refresh_prometheus(memory: Any = None, *, clock: Callable[[], float] = time.monotonic) -> None:
    """Update the gauges at most every 30 seconds; called on each /metrics scrape."""
    with _cache_lock:
        if _cache["snapshot"] is not None and clock() - _cache["at"] < SNAPSHOT_CACHE_SECONDS:
            return
        _cache["at"] = clock()
    try:
        if memory is None:
            from ts_harness.memory.client import get_memory_client

            memory = get_memory_client()
        snapshot = memory.get_scale_metrics(window_seconds=SNAPSHOT_CACHE_SECONDS * 4)
    except Exception as exc:  # noqa: BLE001
        log.debug("scale-out metrics unavailable: %s", exc)
        return
    with _cache_lock:
        _cache["snapshot"] = snapshot
    observe_snapshot(snapshot)

