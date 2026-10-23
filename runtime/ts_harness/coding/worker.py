"""Run one coding job: fetch its spec, run OpenCode, heartbeat, report the result.

The worker holds only its job token, which the memory API accepts on this
job's spec, heartbeat and complete endpoints.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

HEARTBEAT_SECONDS = 30
_LAST_LINE_BYTES = 4096


class JobApi:
    def __init__(self, base_url: str, job_id: str, token: str, *, timeout: float = 30, attempts: int = 3):
        self.base_url = base_url.rstrip("/")
        self.job_id = job_id
        self.token = token
        self.timeout = timeout
        self.attempts = max(1, attempts)

    def _request(self, method: str, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, default=str).encode()
        request = urllib.request.Request(
            f"{self.base_url}/coding-jobs/{self.job_id}/{action}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"memory API unreachable: {last}")

    def spec(self) -> dict[str, Any]:
        return self._request("GET", "spec")

    def registries(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "registries").get("registries") or [])

    def heartbeat(self, last_output: str | None) -> str:
        return str(self._request("POST", "heartbeat", {"last_output": last_output}).get("status") or "")

    def complete(self, *, result: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
        return self._request("POST", "complete", {"result": result, "error": error})


def last_log_line(path: Path) -> str | None:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LAST_LINE_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    return lines[-1][:500] if lines else None


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


def _exit_now() -> None:
    os._exit(143)


def _configure_registries(api: JobApi, home: Path) -> list[str]:
    from ts_harness.coding.registries import write_registry_config

    try:
        registries = api.registries()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    return write_registry_config(registries, home)


def run_job(
    api: JobApi,
    *,
    run: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    on_cancel: Callable[[], None] = _exit_now,
    home: Path | None = None,
) -> int:
    """Run the job and report it; returns the process exit code."""
    job = api.spec()
    if job.get("status") != "running":
        _log("coding_job_not_runnable", job_id=api.job_id, status=job.get("status"))
        return 0
    spec = dict(job.get("spec") or {})
    try:
        configured = _configure_registries(api, home or Path(os.environ.get("HOME") or "/root"))
    except Exception as exc:  # noqa: BLE001
        api.complete(error=f"package registry setup failed: {exc}")
        _log("coding_job_registry_setup_failed", job_id=api.job_id, error=str(exc)[:300])
        return 1
    if configured:
        _log("coding_job_registries_configured", job_id=api.job_id, types=configured)
    if run is None:
        from ts_harness.implementation.coding_job import run_coding_request

        def run(spec: dict[str, Any]) -> dict[str, Any]:
            return run_coding_request(spec, record_refs=False)
    from ts_harness.implementation.backends.opencode import opencode_progress_log_path

    log_path = opencode_progress_log_path(Path(str(spec.get("workspace_dir") or "/workspace")), str(spec.get("work_order_id") or ""))
    stop = threading.Event()
    cancelled = threading.Event()

    def beat() -> None:
        while not stop.wait(heartbeat_seconds):
            try:
                status = api.heartbeat(last_log_line(log_path))
            except Exception as exc:  # noqa: BLE001
                _log("coding_job_heartbeat_failed", job_id=api.job_id, error=str(exc))
                continue
            if status == "cancelled":
                cancelled.set()
                _log("coding_job_cancelled", job_id=api.job_id)
                on_cancel()
                return

    threading.Thread(target=beat, name="coding-job-heartbeat", daemon=True).start()
    _log("coding_job_started", job_id=api.job_id, work_order_id=spec.get("work_order_id"))
    try:
        result = run(spec)
    except Exception as exc:  # noqa: BLE001
        stop.set()
        if not cancelled.is_set():
            api.complete(error=f"coding worker error: {exc}")
        _log("coding_job_crashed", job_id=api.job_id, error=str(exc))
        return 1
    stop.set()
    if cancelled.is_set():
        return 0
    api.complete(result=result)
    _log("coding_job_finished", job_id=api.job_id, ok=bool(result.get("ok")), pr_url=result.get("pr_url"))
    return 0


def main(environ: dict[str, str] | None = None) -> int:
    env = environ if environ is not None else dict(os.environ)
    missing = [name for name in ("JOB_ID", "JOB_TOKEN", "MEMORY_API_URL") if not env.get(name)]
    if missing:
        _log("coding_worker_misconfigured", missing=missing)
        return 2
    return run_job(JobApi(env["MEMORY_API_URL"], env["JOB_ID"], env["JOB_TOKEN"]))
