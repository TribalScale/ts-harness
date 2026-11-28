"""Production alerts: check the harness every few minutes and tell the ops Slack channel.

The ``check_alerts`` system job runs each detector below, then posts the alerts that are new,
or still firing ``TS_HARNESS_ALERT_REPEAT_HOURS`` after they were last posted, and (unless
``TS_HARNESS_ALERT_RESOLVED=false``) one "resolved" line when a condition clears. Which alert
was posted when is kept in ``runtime_kv`` (namespace ``ops-alerts``), so a restart or a new
leader replica does not post everything again.

Each alert has a stable key, ``<detector>:<subject>``. When a detector itself fails (the memory
API is down, say) its alerts are left as they were rather than reported as resolved.

Thresholds come from ``TS_HARNESS_ALERT_*`` environment variables; 0 turns that check off.
The job does nothing (``{"status": "skipped"}``) unless an ops channel and Slack bot token,
or ``SLACK_OPS_WEBHOOK_URL``, are configured. See docs/runbooks/ops-alerts.md.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

logger = logging.getLogger(__name__)

STATE_NAMESPACE = "ops-alerts"
STATE_KEY = "state"
CODING_STEPS = ("deliver_work_order", "implement")

# Kept in this process too, so de-duplication still works while the memory API is down.
_local_state: dict[str, Any] = {}


@dataclass(frozen=True)
class Alert:
    key: str
    text: str
    resolved: str = ""

    @property
    def detector(self) -> str:
        return self.key.split(":", 1)[0]


@dataclass(frozen=True)
class AlertSettings:
    repeat_hours: float = 6
    stuck_hours: float = 4
    lost_jobs: int = 2
    revision_runs: int = 5
    hourly_usd: float = 20
    daily_usd: float = 150
    disk_percent: float = 85
    disk_path: str = "/"
    post_resolved: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AlertSettings:
        env = dict(os.environ if env is None else env)
        defaults = cls()

        def number(name: str, default: float) -> float:
            raw = str(env.get(f"TS_HARNESS_ALERT_{name}") or "").strip()
            if not raw:
                return default
            try:
                return max(0.0, float(raw))
            except ValueError:
                logger.warning("TS_HARNESS_ALERT_%s=%r is not a number; using %s", name, raw, default)
                return default

        resolved = str(env.get("TS_HARNESS_ALERT_RESOLVED") or "true").strip().lower()
        return cls(
            repeat_hours=number("REPEAT_HOURS", defaults.repeat_hours),
            stuck_hours=number("STUCK_HOURS", defaults.stuck_hours),
            lost_jobs=int(number("LOST_JOBS", defaults.lost_jobs)),
            revision_runs=int(number("REVISION_RUNS", defaults.revision_runs)),
            hourly_usd=number("HOURLY_USD", defaults.hourly_usd),
            daily_usd=number("DAILY_USD", defaults.daily_usd),
            disk_percent=number("DISK_PERCENT", defaults.disk_percent),
            disk_path=str(env.get("TS_HARNESS_ALERT_DISK_PATH") or defaults.disk_path),
            post_resolved=resolved not in {"0", "false", "no", "off"},
        )


def _hours(seconds: float) -> str:
    hours = seconds / 3600
    return f"{hours:.0f} h" if hours >= 1.5 else f"{max(1, round(seconds / 60))} min"


def _task_label(row: dict[str, Any]) -> str:
    task_input = row.get("input") if isinstance(row.get("input"), dict) else {}
    for key in ("work_order_id", "jira_issue_key", "work_order_ref"):
        value = str(task_input.get(key) or row.get(key) or "").strip()
        if value:
            return value
    return f"{row.get('workflow_type') or 'workflow'} {str(row.get('workflow_id') or '')[:8]}".strip()


# ---------------------------------------------------------------------------
# Detectors: each returns the alerts firing now and raises when it cannot tell.
# ---------------------------------------------------------------------------


def detect_stuck_steps(memory: Any, settings: AlertSettings, now: datetime, *, tenant_id: str = "default") -> list[Alert]:
    """Steps waiting for a person's decision longer than ``stuck_hours``."""
    if not settings.stuck_hours:
        return []
    from ts_harness.workflows.live_view import _parse
    from ts_harness.workflows.ticket_story import WAIT_REASONS
    from ts_harness.workflows.work_order_waves import HUMAN_DECISION_KEY

    from ts_harness.workflows.pause import without_paused

    alerts = []
    # Time a ticket spends paused is not stuck work: a person chose to hold it.
    for row in without_paused(memory, list(memory.list_stuck_tasks(tenant_id=tenant_id) or [])):
        task_input = row.get("input") if isinstance(row.get("input"), dict) else {}
        waiting = task_input.get(HUMAN_DECISION_KEY) if isinstance(task_input.get(HUMAN_DECISION_KEY), dict) else {}
        since = _parse(waiting.get("since")) or _parse(row.get("updated_at"))
        if since is None:
            continue
        waited = (now - since).total_seconds()
        if waited < settings.stuck_hours * 3600:
            continue
        reason = WAIT_REASONS.get(str(waiting.get("reason") or ""), "")
        if not reason:
            error = " ".join(str(row.get("error") or "").split())
            reason = error[:120] if error else "a decision is needed"
        label = _task_label(row)
        alerts.append(
            Alert(
                key=f"stuck:{row.get('id')}",
                text=f"{label} has waited {_hours(waited)} for a decision at `{row.get('step_name')}` ({reason}).",
                resolved=f"{label} is no longer waiting for a decision at `{row.get('step_name')}`.",
            )
        )
    return alerts


def detect_lost_jobs(memory: Any, settings: AlertSettings, now: datetime) -> list[Alert]:
    """``lost_jobs`` or more coding jobs marked lost in the last hour."""
    if not settings.lost_jobs:
        return []
    jobs = memory.list_coding_jobs(status="lost", finished_within=3600, limit=200)
    if len(jobs) < settings.lost_jobs:
        return []
    labels = sorted({str(job.get("work_order_id") or str(job.get("id") or "")[:8]) for job in jobs})
    shown = ", ".join(labels[:5]) + (f" and {len(labels) - 5} more" if len(labels) > 5 else "")
    return [
        Alert(
            key="lost_jobs:last_hour",
            text=(
                f"{len(jobs)} coding jobs were lost in the last hour ({shown}). "
                "Their workers stopped heartbeating or overran; check the coding launcher and worker logs."
            ),
            resolved="Coding jobs are no longer being lost (fewer than "
            f"{settings.lost_jobs} in the last hour).",
        )
    ]


def detect_non_converging(memory: Any, settings: AlertSettings, now: datetime, *, tenant_id: str = "default") -> list[Alert]:
    """Running work whose coding step has run ``revision_runs`` or more times."""
    if not settings.revision_runs:
        return []
    rows = memory.list_repeated_step_runs(tenant_id=tenant_id, steps=list(CODING_STEPS), min_runs=settings.revision_runs)
    alerts = []
    for row in rows:
        label = _task_label(row)
        alerts.append(
            Alert(
                key=f"revisions:{row.get('workflow_id')}",
                text=(
                    f"{label} is not converging: its coding step has run {row.get('runs')} times "
                    "and the work order is still open."
                ),
                resolved=f"{label} finished or was stopped after repeated coding runs.",
            )
        )
    return alerts


def _spend(memory: Any, window_seconds: int) -> float:
    snapshot = memory.get_scale_metrics(window_seconds=max(10, min(int(window_seconds), 86400)))
    return round(sum(float(row.get("window_cost_usd") or 0) for row in snapshot.get("llm") or []), 2)


def detect_spend(memory: Any, settings: AlertSettings, now: datetime) -> list[Alert]:
    """LLM spend in the last hour, and today (UTC), above the thresholds."""
    alerts = []
    if settings.hourly_usd:
        hour = _spend(memory, 3600)
        if hour > settings.hourly_usd:
            alerts.append(
                Alert(
                    key="spend:hour",
                    text=f"LLM spend in the last hour is ${hour:,.2f} (alert above ${settings.hourly_usd:,.2f}).",
                    resolved=f"LLM spend in the last hour is back under ${settings.hourly_usd:,.2f}.",
                )
            )
    if settings.daily_usd:
        midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        today = _spend(memory, int((now - midnight).total_seconds()))
        if today > settings.daily_usd:
            alerts.append(
                Alert(
                    key="spend:day",
                    text=f"LLM spend today (UTC) is ${today:,.2f} (alert above ${settings.daily_usd:,.2f}).",
                    resolved=f"LLM spend for the day is back under ${settings.daily_usd:,.2f} (a new UTC day began).",
                )
            )
    return alerts


def detect_disk(settings: AlertSettings, *, disk_usage: Callable[[str], Any] = shutil.disk_usage) -> list[Alert]:
    if not settings.disk_percent:
        return []
    usage = disk_usage(settings.disk_path)
    percent = 100.0 * usage.used / usage.total if usage.total else 0.0
    if percent <= settings.disk_percent:
        return []
    return [
        Alert(
            key=f"disk:{settings.disk_path}",
            text=(
                f"Disk {settings.disk_path} is {percent:.0f}% full ({usage.free / 1e9:.1f} GB free; "
                f"alert above {settings.disk_percent:.0f}%). Prune Docker images and build cache."
            ),
            resolved=f"Disk {settings.disk_path} is back under {settings.disk_percent:.0f}% full.",
        )
    ]


def _http_ok(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - configured service URL
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def detect_service_health(memory: Any, *, http_ok: Callable[[str], tuple[bool, str]] = _http_ok) -> list[Alert]:
    """The memory API and, when the runtime uses it, the LLM proxy answer their health checks."""
    from ts_harness.llm_proxy.tokens import proxy_url

    alerts = []
    if not memory.health():
        alerts.append(
            Alert(
                key="health:memory_api",
                text="The memory API is not answering its health check; workflows, Inbox and coding jobs are affected.",
                resolved="The memory API is answering its health check again.",
            )
        )
    url = proxy_url()
    if url:
        ok, detail = http_ok(f"{url}/health")
        if not ok:
            alerts.append(
                Alert(
                    key="health:llm_proxy",
                    text=f"The LLM proxy is not answering its health check ({detail}); model calls fail.",
                    resolved="The LLM proxy is answering its health check again.",
                )
            )
    return alerts


def run_detectors(
    memory: Any,
    settings: AlertSettings,
    now: datetime,
    *,
    tenant_id: str = "default",
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
    http_ok: Callable[[str], tuple[bool, str]] = _http_ok,
) -> tuple[list[Alert], set[str]]:
    """Alerts firing now, and the detectors that failed (their earlier alerts stay as they were)."""
    detectors: list[tuple[str, Callable[[], list[Alert]]]] = [
        ("stuck", lambda: detect_stuck_steps(memory, settings, now, tenant_id=tenant_id)),
        ("lost_jobs", lambda: detect_lost_jobs(memory, settings, now)),
        ("revisions", lambda: detect_non_converging(memory, settings, now, tenant_id=tenant_id)),
        ("spend", lambda: detect_spend(memory, settings, now)),
        ("disk", lambda: detect_disk(settings, disk_usage=disk_usage)),
        ("health", lambda: detect_service_health(memory, http_ok=http_ok)),
    ]
    alerts: list[Alert] = []
    failed: set[str] = set()
    for name, detect in detectors:
        try:
            alerts.extend(detect())
        except Exception as exc:  # noqa: BLE001
            failed.add(name)
            logger.warning("ops alert check %s failed: %s", name, exc)
    return alerts, failed


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def plan_posts(
    state: dict[str, Any], alerts: list[Alert], failed: set[str], now: datetime, settings: AlertSettings
) -> tuple[dict[str, Any], list[str]]:
    """The new state and the lines to post: new alerts, repeats past ``repeat_hours``, resolutions."""
    previous = dict((state or {}).get("active") or {})
    active: dict[str, Any] = {}
    lines: list[str] = []
    repeat = timedelta(hours=settings.repeat_hours)
    for alert in alerts:
        if alert.key in active:
            continue
        prior = previous.get(alert.key) or {}
        last = _parse_iso(prior.get("last_posted"))
        entry = {
            "first_seen": prior.get("first_seen") or now.isoformat(),
            "last_posted": prior.get("last_posted") or "",
            "text": alert.text,
            "resolved": alert.resolved,
        }
        if last is None or now - last >= repeat:
            entry["last_posted"] = now.isoformat()
            lines.append(alert.text if last is None else f"Still firing: {alert.text}")
        active[alert.key] = entry
    for key, prior in previous.items():
        if key in active:
            continue
        if key.split(":", 1)[0] in failed:
            active[key] = prior
            continue
        if settings.post_resolved and prior.get("last_posted"):
            lines.append(f"Resolved: {prior.get('resolved') or prior.get('text')}")
    return {"active": active, "checked_at": now.isoformat()}, lines


def _load_and_plan(memory: Any, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Apply ``change`` to the shared state (compare-and-swap), or to the local copy when it is unreachable."""
    global _local_state
    try:
        from ts_harness.storage.kv import kv_update

        _local_state = kv_update(STATE_NAMESPACE, STATE_KEY, change, default={}, client=memory)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ops alert state unavailable, using this process's copy: %s", exc)
        _local_state = change(dict(_local_state))


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def ops_target(config: Any = None) -> dict[str, str]:
    """Where alerts go: the ops channel through the Slack bot, else the ops webhook; empty when neither."""
    channel = ""
    budgets = getattr(config, "budgets", None)
    if budgets is not None:
        channel = str(getattr(budgets, "notify_channel", "") or "")
    channel = channel or os.environ.get("SLACK_OPS_CHANNEL", "").strip()
    if channel and os.environ.get("SLACK_BOT_TOKEN", "").strip():
        return {"channel": channel}
    webhook = os.environ.get("SLACK_OPS_WEBHOOK_URL", "").strip()
    if webhook:
        return {"webhook": webhook}
    return {}


def _post_webhook(url: str, text: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps({"text": text}).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - operator-configured webhook
            return {"ok": 200 <= response.status < 300}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def post_ops_text(text: str, target: dict[str, str], *, notifier: Any = None) -> dict[str, Any]:
    if target.get("channel"):
        if notifier is None:
            from ts_harness.notifications.slack import SlackNotifier

            notifier = SlackNotifier()
        return notifier.post_thread_text(text=text, channel=target["channel"])
    if target.get("webhook"):
        return _post_webhook(target["webhook"], text)
    return {"ok": False, "error": "no_target"}


def check_alerts(
    *,
    tenant_id: str = "default",
    memory: Any = None,
    config: Any = None,
    notifier: Any = None,
    settings: AlertSettings | None = None,
    now: datetime | None = None,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
    http_ok: Callable[[str], tuple[bool, str]] = _http_ok,
) -> dict[str, Any]:
    """System job: run the detectors and post new, repeated and resolved alerts to the ops channel."""
    if config is None:
        try:
            from ts_harness.gateway.server import get_runtime

            config = get_runtime().config
        except Exception:  # noqa: BLE001 - the env vars alone can configure the target
            config = None
    target = ops_target(config)
    if not target:
        return {"status": "skipped", "reason": "no ops channel (with SLACK_BOT_TOKEN) or SLACK_OPS_WEBHOOK_URL"}
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    settings = settings or AlertSettings.from_env()
    now = now or datetime.now(UTC)
    alerts, failed = run_detectors(memory, settings, now, tenant_id=tenant_id, disk_usage=disk_usage, http_ok=http_ok)

    planned: dict[str, list[str]] = {"lines": []}

    def change(state: dict[str, Any]) -> dict[str, Any]:
        updated, planned["lines"] = plan_posts(state, alerts, failed, now, settings)
        return updated

    _load_and_plan(memory, change)
    lines = planned["lines"]
    sent = 0
    if lines:
        text = "*TS Harness alerts*\n" + "\n".join(f"• {line}" for line in lines)
        result = post_ops_text(text, target, notifier=notifier)
        if result.get("ok"):
            sent = len(lines)
        else:
            logger.warning("ops alert post failed: %s", result.get("error"))
            # Forget this round's posts so the next run tries again instead of waiting out the repeat window.
            fired = {alert.key for alert in alerts}

            def unpost(state: dict[str, Any]) -> dict[str, Any]:
                active = dict(state.get("active") or {})
                for key in fired & set(active):
                    if active[key].get("last_posted") == now.isoformat():
                        active[key] = {**active[key], "last_posted": ""}
                return {**state, "active": active}

            _load_and_plan(memory, unpost)
    for alert in alerts:
        logger.info("ops alert firing key=%s", alert.key)
    return {
        "status": "ok",
        "firing": sorted(alert.key for alert in alerts),
        "failed_checks": sorted(failed),
        "posted": sent,
        "lines": lines,
    }
