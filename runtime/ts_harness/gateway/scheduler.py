"""Cron and heartbeat scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
import json
import os
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from ts_harness.config.schema import RuntimeConfig

SCHEDULER_STATE_NAMESPACE = "scheduler"
SCHEDULER_STATE_KEY = "state"


@dataclass
class ScheduledJob:
    agent_id: str
    kind: str  # heartbeat | cron | system
    interval_seconds: int
    prompt: str
    name: str | None = None
    cron: str | None = None
    announce: dict[str, Any] | None = None
    handler: str | None = None
    skill_id: str | None = None
    skill_hints: list[str] | None = None
    program: dict[str, Any] | None = None
    active_start: str = "07:00"
    active_end: str = "22:00"
    timezone: str = "UTC"
    last_run: float = 0.0


def _normalize_skill_hints(raw: Mapping[str, Any] | dict[str, Any]) -> tuple[str | None, list[str]]:
    skill_id = str(raw.get("skill_id") or "").strip() or None
    hints: list[str] = []
    for item in list(raw.get("skill_hints") or []):
        value = str(item).strip()
        if value and value not in hints:
            hints.append(value)
    if skill_id and skill_id not in hints:
        hints.insert(0, skill_id)
    return skill_id, hints


def _parse_interval(every: str) -> int:
    every = every.strip().lower()
    if every.endswith("m"):
        return int(every[:-1]) * 60
    if every.endswith("h"):
        return int(every[:-1]) * 3600
    return int(every)


def _within_active_hours(job: ScheduledJob, now: datetime) -> bool:
    try:
        local_now = now.astimezone(ZoneInfo(job.timezone))
    except ZoneInfoNotFoundError:
        local_now = now.astimezone(UTC)
    start_h, start_m = [int(x) for x in job.active_start.split(":")]
    end_h, end_m = [int(x) for x in job.active_end.split(":")]
    minute = local_now.hour * 60 + local_now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    return start <= minute <= end


class Scheduler:
    def __init__(
        self,
        config: RuntimeConfig,
        dispatch: Callable[[dict[str, Any]], None],
        *,
        workspace_root: Path | None = None,
        cron_jobs: list[dict[str, Any]] | None = None,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
        state_path: Path | None = None,
        leader: Callable[[], bool] | None = None,
        cron_path: Path | None = None,
    ):
        self.config = config
        # With several runtime replicas only the lease holder runs jobs, and an
        # edit made through another replica reaches it through the cron file.
        self.leader = leader
        self.cron_path = cron_path
        self._cron_mtime = self._cron_file_mtime()
        self.dispatch = dispatch
        self.workspace_root = workspace_root or Path("/workspace")
        self.audit_sink = audit_sink
        self.state_path = state_path
        self._state = self._load_state()
        self._was_leading = False
        self.jobs: list[ScheduledJob] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started_at = time.time()
        self._build_jobs(cron_jobs or [])

    def _build_jobs(self, cron_jobs: list[dict[str, Any]]) -> None:
        self._cron_raw: list[dict[str, Any]] = [dict(item) for item in cron_jobs]
        for agent in self.config.agents:
            if not agent.heartbeat:
                continue
            hb = agent.heartbeat
            self.jobs.append(
                ScheduledJob(
                    agent_id=agent.id,
                    name=f"heartbeat:{agent.id}",
                    kind="heartbeat",
                    interval_seconds=_parse_interval(hb.every),
                    prompt=hb.prompt,
                    active_start=hb.active_hours_start,
                    active_end=hb.active_hours_end,
                    timezone=hb.timezone,
                )
            )
        for raw in cron_jobs:
            kind = str(raw.get("kind") or "cron").strip() or "cron"
            if kind not in {"cron", "system"}:
                kind = "cron"
            skill_id, skill_hints = _normalize_skill_hints(raw)
            self.jobs.append(
                ScheduledJob(
                    agent_id=str(raw.get("agent_id", self.config.default_agent().id)),
                    name=str(raw.get("name", raw.get("agent_id", "cron"))),
                    kind=kind,
                    interval_seconds=int(raw.get("interval_seconds", 60)),
                    cron=raw.get("cron"),
                    announce=raw.get("announce"),
                    handler=str(raw.get("handler") or "").strip() or None,
                    skill_id=skill_id,
                    skill_hints=skill_hints,
                    program=dict(raw["program"]) if isinstance(raw.get("program"), dict) else None,
                    prompt=str(raw.get("prompt", "")),
                    active_start=str(raw.get("active_start", "00:00")),
                    active_end=str(raw.get("active_end", "23:59")),
                    timezone=str(raw.get("timezone", "UTC")),
                )
            )

    def replace_cron_jobs(self, cron_jobs: list[dict[str, Any]]) -> None:
        """Hot-reload file-backed cron/system jobs; preserve heartbeats."""
        heartbeats = [job for job in self.jobs if job.kind == "heartbeat"]
        self.jobs = list(heartbeats)
        self._cron_raw = []
        for raw in cron_jobs:
            kind = str(raw.get("kind") or "cron").strip() or "cron"
            if kind not in {"cron", "system"}:
                kind = "cron"
            skill_id, skill_hints = _normalize_skill_hints(raw)
            entry = dict(raw)
            entry["kind"] = kind
            if skill_id:
                entry["skill_id"] = skill_id
            if skill_hints:
                entry["skill_hints"] = skill_hints
            self._cron_raw.append(entry)
            self.jobs.append(
                ScheduledJob(
                    agent_id=str(raw.get("agent_id", self.config.default_agent().id)),
                    name=str(raw.get("name", raw.get("agent_id", "cron"))),
                    kind=kind,
                    interval_seconds=int(raw.get("interval_seconds", 60)),
                    cron=raw.get("cron"),
                    announce=raw.get("announce"),
                    handler=str(raw.get("handler") or "").strip() or None,
                    skill_id=skill_id,
                    skill_hints=skill_hints,
                    program=dict(raw["program"]) if isinstance(raw.get("program"), dict) else None,
                    prompt=str(raw.get("prompt", "")),
                    active_start=str(raw.get("active_start", "00:00")),
                    active_end=str(raw.get("active_end", "23:59")),
                    timezone=str(raw.get("timezone", "UTC")),
                )
            )

    def list_cron_jobs(self) -> list[dict[str, Any]]:
        """Return file-backed cron/system job dicts enriched with runtime fields."""
        now = datetime.now(UTC)
        by_name = {
            job.name: job
            for job in self.jobs
            if job.kind in {"cron", "system"} and job.name
        }
        result: list[dict[str, Any]] = []
        for raw in getattr(self, "_cron_raw", []):
            name = str(raw.get("name") or "")
            job = by_name.get(name)
            last_run = None
            if job:
                last_run = self._state.get(job.name or job.agent_id, {}).get("last_run")
            next_run = None
            cron_expr = raw.get("cron")
            tz = str(raw.get("timezone") or "UTC")
            if cron_expr:
                try:
                    local_now = now.astimezone(ZoneInfo(tz))
                except ZoneInfoNotFoundError:
                    local_now = now.astimezone(UTC)
                try:
                    nxt = croniter(str(cron_expr), local_now).get_next(datetime)
                    next_run = nxt.astimezone(UTC).isoformat()
                except (ValueError, KeyError):
                    next_run = None
            result.append(
                {
                    **raw,
                    "last_run": last_run,
                    "next_run": next_run,
                }
            )
        return result

    def trigger_job(self, name: str) -> dict[str, Any]:
        """Force-dispatch a single cron/system job by name."""
        target = str(name or "").strip()
        if not target:
            raise ValueError("job name is required")
        job = next(
            (j for j in self.jobs if j.kind in {"cron", "system"} and j.name == target),
            None,
        )
        if job is None:
            raise KeyError(f"cron job not found: {target}")
        now = datetime.now(UTC)
        job.last_run = time.time()
        self._state[job.name or job.agent_id] = {"last_run": job.last_run}
        self._save_state()
        lane = "system" if job.kind == "system" else "cron"
        payload: dict[str, Any] = {
            "agent_id": job.agent_id,
            "kind": job.kind,
            "prompt": job.prompt,
            "source": "cron",
            "lane": lane,
            "forced": True,
            "name": job.name,
        }
        if job.handler:
            payload["handler"] = job.handler
        if job.announce:
            payload["announce"] = job.announce
        if job.skill_id:
            payload["skill_id"] = job.skill_id
        if job.skill_hints:
            payload["skill_hints"] = list(job.skill_hints)
        if job.program:
            payload["program"] = dict(job.program)
        self.dispatch(payload)
        self._audit({"event": "cron_job_triggered", "name": target, "at": now.isoformat()})
        return {"status": "ok", "name": target, "dispatched": True}

    def start(self) -> None:
        if os.environ.get("TS_HARNESS_SCHEDULER_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ec-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(5)

    def tick(self) -> bool:
        """One pass of the loop; returns whether this replica led it."""
        leading = self.leader is None or self.leader()
        if leading:
            if not self._was_leading:
                # Another replica may have run jobs while it held the lease.
                self._state = self._merge_state(self._load_state(), self._state)
            from ts_harness.storage.shared_tree import sync_cron

            sync_cron()
            self.reload_cron_file_if_changed()
            self.run_due_once(now=datetime.now(UTC))
        self._was_leading = leading
        return leading

    def _cron_file_mtime(self) -> float | None:
        try:
            return self.cron_path.stat().st_mtime if self.cron_path else None
        except OSError:
            return None

    def reload_cron_file_if_changed(self) -> bool:
        mtime = self._cron_file_mtime()
        if mtime is None or mtime == self._cron_mtime:
            return False
        try:
            data = json.loads(self.cron_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self._cron_mtime = mtime
        self.replace_cron_jobs(list(data.get("jobs") or []))
        return True

    def health(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def expected_interval_seconds(self, job: ScheduledJob, now: datetime) -> float:
        """Seconds between consecutive runs; cron jobs use the gap around ``now``."""
        if not job.cron:
            return float(max(1, job.interval_seconds))
        try:
            local_now = now.astimezone(ZoneInfo(job.timezone))
        except ZoneInfoNotFoundError:
            local_now = now.astimezone(UTC)
        previous = croniter(job.cron, local_now).get_prev(datetime)
        following = croniter(job.cron, local_now).get_next(datetime)
        return max(60.0, (following - previous).total_seconds())

    def stale_jobs(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Cron/system jobs that have not run for more than two expected intervals.

        A job with no recorded run is only stale once the scheduler itself has been
        up for two intervals, so a fresh boot does not alarm.
        """
        now = now or datetime.now(UTC)
        now_ts = now.timestamp()
        stale: list[dict[str, Any]] = []
        for job in self.jobs:
            if job.kind not in {"cron", "system"} or not job.name:
                continue
            try:
                interval = self.expected_interval_seconds(job, now)
            except (ValueError, KeyError):
                continue
            last_run = float(self._state.get(job.name, {}).get("last_run") or job.last_run or 0)
            reference = last_run or self.started_at
            overdue = now_ts - reference - 2 * interval
            if overdue <= 0:
                continue
            stale.append(
                {
                    "name": job.name,
                    "kind": job.kind,
                    "last_run": datetime.fromtimestamp(last_run, UTC).isoformat() if last_run else None,
                    "interval_seconds": int(interval),
                    "overdue_seconds": int(overdue),
                }
            )
        return stale

    def run_due_once(self, *, now: datetime | None = None, force: bool = False) -> None:
        now = now or datetime.now(UTC)
        for job in self.jobs:
            last_run = self._state.get(job.name or job.agent_id, {}).get("last_run", job.last_run)
            if not force and not self._is_due(job, now, float(last_run or 0)):
                continue
            if not _within_active_hours(job, now):
                continue
            if job.kind == "heartbeat" and self._heartbeat_file_empty(job.agent_id):
                continue
            job.last_run = time.time()
            self._state[job.name or job.agent_id] = {"last_run": job.last_run}
            self._save_state()
            if job.kind == "system":
                lane = "system"
            elif job.kind == "cron":
                lane = "cron"
            else:
                lane = "heartbeat"
            payload = {
                "agent_id": job.agent_id,
                "kind": job.kind,
                "prompt": job.prompt,
                "source": "cron",
                "lane": lane,
                "name": job.name,
            }
            if job.handler:
                payload["handler"] = job.handler
            if job.announce:
                payload["announce"] = job.announce
            if job.skill_id:
                payload["skill_id"] = job.skill_id
            if job.skill_hints:
                payload["skill_hints"] = list(job.skill_hints)
            if job.program:
                payload["program"] = dict(job.program)
            def guarded_dispatch() -> None:
                try:
                    self.dispatch(payload)
                except Exception as exc:  # noqa: BLE001
                    self._audit(
                        {
                            "event": "scheduler_dispatch_failed",
                            "agent_id": job.agent_id,
                            "kind": job.kind,
                            "error": str(exc),
                        }
                    )

            thread = threading.Thread(target=guarded_dispatch, daemon=True)
            thread.start()
            thread.join(timeout=0.1)
            if thread.is_alive():
                self._audit({"event": "scheduler_dispatch_backgrounded", "agent_id": job.agent_id})

    def _heartbeat_file_empty(self, agent_id: str) -> bool:
        from ts_harness.storage.workspace import workspace_store

        try:
            text = workspace_store(agent_id, root=self.workspace_root).read_text_or_none("HEARTBEAT.md")
        except Exception as exc:  # noqa: BLE001
            self._audit({"event": "heartbeat_file_read_failed", "agent_id": agent_id, "error": str(exc)[:300]})
            return False
        return text is not None and not text.strip()

    def _audit(self, event: dict[str, Any]) -> None:
        if self.audit_sink:
            self.audit_sink(event)

    def _is_due(self, job: ScheduledJob, now: datetime, last_run: float) -> bool:
        if not last_run:
            return True
        if job.cron:
            try:
                local_now = now.astimezone(ZoneInfo(job.timezone))
            except ZoneInfoNotFoundError:
                local_now = now.astimezone(UTC)
            itr = croniter(job.cron, local_now)
            previous = itr.get_prev(datetime)
            return previous.timestamp() > last_run
        return time.time() - last_run >= job.interval_seconds

    def _previous_cron_time(self, expr: str, now: datetime, timezone: str) -> datetime:
        minute_s, hour_s, day_s, _month_s, weekday_s = expr.split()
        try:
            local_now = now.astimezone(ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            local_now = now.astimezone(UTC)
        minute = int(minute_s)
        hour = int(hour_s)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > local_now:
            candidate -= timedelta(days=1)
        if weekday_s != "*":
            target = int(weekday_s)
            # cron: Sunday=0, Monday=1; Python Monday=0
            py_target = (target - 1) % 7
            while candidate.weekday() != py_target:
                candidate -= timedelta(days=1)
        if day_s != "*":
            target_day = int(day_s)
            while candidate.day != target_day:
                candidate -= timedelta(days=1)
        return candidate.astimezone(UTC)

    def _load_state(self) -> dict[str, dict[str, float]]:
        from ts_harness.storage.backend import shared_state_enabled

        if shared_state_enabled():
            from ts_harness.storage.kv import kv_read

            try:
                return dict(kv_read(SCHEDULER_STATE_NAMESPACE, SCHEDULER_STATE_KEY, default={}))
            except Exception as exc:  # noqa: BLE001
                self._audit({"event": "scheduler_state_load_failed", "error": str(exc)[:300]})
                return dict(getattr(self, "_state", {}) or {})
        if not self.state_path or not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    @staticmethod
    def _merge_state(stored: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
        """Per job, keep whichever record ran last."""
        merged = dict(stored or {})
        for name, entry in local.items():
            if float((merged.get(name) or {}).get("last_run") or 0) <= float((entry or {}).get("last_run") or 0):
                merged[name] = entry
        return merged

    def _save_state(self) -> None:
        from ts_harness.storage.backend import shared_state_enabled

        if shared_state_enabled():
            from ts_harness.storage.kv import kv_update

            try:
                self._state = kv_update(
                    SCHEDULER_STATE_NAMESPACE,
                    SCHEDULER_STATE_KEY,
                    lambda stored: self._merge_state(stored, self._state),
                    default={},
                )
            except Exception as exc:  # noqa: BLE001
                self._audit({"event": "scheduler_state_save_failed", "error": str(exc)[:300]})
            return
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, sort_keys=True), encoding="utf-8")
