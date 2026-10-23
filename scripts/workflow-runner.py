#!/usr/bin/env python3
# flake8: noqa
"""Single-instance workflow runner.

This sidecar is intentionally one runner per instance. It shares the
agent-runtime network namespace so 127.0.0.1 reaches memory-api and runtime
HTTP endpoints; do not scale replicas without changing that deployment model.

Stale lock recovery consumes retry budget: the original claim increments
attempt, and a later retry is bounded by attempt <= max_retries in SQL.

New in this revision:
  - tenant_id forwarded on every /workflow/dispatch call
  - Bounded concurrency via Redis counter or in-process Semaphore
  - Retry backoff: tasks whose next_attempt_at is in the future are skipped
  - Plan approval gating: expand tasks with plan_approval_required block
    until the proposed plan is approved
  - Workflow record creation on first-seen workflow_id
  - Leader election via Redis SETNX on key TS_HARNESS_RUNNER_LEADER
"""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
import contextvars
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
RUNTIME_DIR = REPO_ROOT / "runtime"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import workflow_definitions  # noqa: E402
from ts_harness.workflows.definition_paths import (  # noqa: E402
    ensure_history_snapshot,
    list_history_paths_for_version,
    safe_component,
)
from ts_harness.workflows.wait_deadlines import wait_deadlines  # noqa: E402
from ts_harness.tools.approvals import AWAITING_TOOL_APPROVAL_PREFIX  # noqa: E402


DEFAULT_MEMORY_API_URL = "http://127.0.0.1:18792"
DEFAULT_TICK_FILE = Path("/tmp/workflow-runner.tick")
METRICS_FILE = Path(os.environ.get("WORKFLOW_RUNNER_METRICS_FILE", "/tmp/workflow-runner.metrics.json"))


class RunnerMetrics:
    def __init__(self) -> None:
        self.counters: dict[str, float] = {}
        self.observations: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            bucket = self.observations.setdefault(name, [])
            bucket.append(value)
            if len(bucket) > 100:
                del bucket[:-100]

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            result = dict(self.counters)
            for name, values in self.observations.items():
                if values:
                    result[f"{name}_last"] = values[-1]
                    result[f"{name}_avg"] = sum(values) / len(values)
            return result

    def persist(self) -> None:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **self.snapshot(),
        }
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        METRICS_FILE.write_text(json.dumps(payload), encoding="utf-8")


_runner_metrics = RunnerMetrics()
_loaded_versioned_skeletons: dict[
    tuple[str, str],
    workflow_definitions.WorkflowSkeleton,
] = {}


def qa_review_evidence(task: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """PR diff already gathered by review_precheck, for a QA task that lacks it.

    Tasks hydrated before the fragment passed ``files`` still need the patch,
    or QA fails closed and sends the PR back for a revision that changes nothing.
    """
    if str(task.get("step_name") or "") != "qa_gate":
        return {}
    current = task.get("input") if isinstance(task.get("input"), dict) else {}
    if current.get("files"):
        return {}
    candidates = [
        item
        for item in tasks
        if item.get("step_name") == "review_precheck" and item.get("status") == "succeeded"
    ]
    if not candidates:
        return {}
    latest = sorted(candidates, key=lambda item: str(item.get("created_at") or ""))[-1]
    output = latest.get("output") if isinstance(latest.get("output"), dict) else {}
    evidence: dict[str, Any] = {}
    for key in ("files", "diff_stats", "prs"):
        value = output.get(key)
        if value not in (None, "", [], {}):
            evidence[key] = value
    return evidence


def _definition_store_root() -> Path | None:
    store_root = os.environ.get(
        "TS_HARNESS_WORKFLOW_DEFINITION_STORE",
        "",
    ).strip()
    return Path(store_root) if store_root else None


def _managed_active_path(workflow_type: str) -> Path | None:
    safe_type = safe_component(workflow_type, field="workflow_type")
    store_root = _definition_store_root()
    if store_root is not None:
        return store_root / "active" / f"{safe_type}.json"
    managed_dir = os.environ.get("TS_HARNESS_MANAGED_WORKFLOWS_DIR", "").strip()
    if managed_dir:
        return Path(managed_dir) / f"{safe_type}.json"
    return None


def _has_managed_active(workflow_type: str) -> bool:
    path = _managed_active_path(workflow_type)
    return path is not None and path.exists()


def _ensure_history_snapshot(skeleton: workflow_definitions.WorkflowSkeleton) -> None:
    store_root = _definition_store_root()
    if store_root is None:
        return
    try:
        ensure_history_snapshot(store_root, skeleton)
    except (OSError, PermissionError) as exc:
        log_event(
            "workflow_history_snapshot_failed",
            workflow_type=skeleton.workflow_type,
            definition_version=skeleton.definition_version,
            error=str(exc),
        )


def log_event(event: str, **details: Any) -> None:
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    payload.update(details)
    try:
        from ts_harness.observability.logging import current_trace_id

        trace_id = current_trace_id()
    except Exception:
        trace_id = ""
    if trace_id and "trace_id" not in payload:
        payload["trace_id"] = trace_id
    print(json.dumps(payload, default=str), flush=True)


def _workflow_engine() -> Any:
    from ts_harness.workflows.engine import get_workflow_engine

    engine = get_workflow_engine()
    engine.reload_if_changed()
    try:
        from ts_harness.workflows.program_registry import (
            get_program_definition_registry,
        )

        # Snapshot-driven completion/advance uses pinned program rows; reload
        # only keeps the registry warm for any start-side paths in-process.
        get_program_definition_registry().reload_if_changed()
    except Exception:
        pass
    return engine


HTTP_RETRY_ATTEMPTS = 3
HTTP_RETRY_BASE_SECONDS = 0.5
TRANSPORT_ERROR_PREFIX = "transport_error:"


def compute_poll_backoff(base_interval: int, failures: int, *, cap: int = 60) -> int:
    """Return poll sleep seconds after *failures* consecutive tick errors."""
    if failures <= 0:
        return base_interval
    return min(cap, base_interval * (2 ** min(failures, 3)))


def _http_request_with_retries(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    max_attempts: int = HTTP_RETRY_ATTEMPTS,
    retry_base_seconds: float = HTTP_RETRY_BASE_SECONDS,
) -> bytes:
    """Perform an HTTP request with bounded retries for transport/5xx failures."""
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if 500 <= int(exc.code) < 600 and attempt + 1 < max_attempts:
                last_error = exc
                time.sleep(retry_base_seconds * (2**attempt))
                continue
            detail = exc.read().decode() or str(exc)
            raise RuntimeError(detail) from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(retry_base_seconds * (2**attempt))
                continue
            raise RuntimeError(str(exc.reason if getattr(exc, "reason", None) else exc)) from exc
        except TimeoutError as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(retry_base_seconds * (2**attempt))
                continue
            raise RuntimeError(str(exc)) from exc
    raise RuntimeError(str(last_error) if last_error else "http request failed")


# ---------------------------------------------------------------------------
# Concurrency limiter
# ---------------------------------------------------------------------------

class ConcurrencyLimiter:
    """In-process Semaphore or Redis-backed active-task counter.

    When *redis_url* is supplied the counter lives in Redis under
    ``ts_harness:runner:active`` so multiple processes sharing the same
    Redis instance respect a shared limit.  Falls back to a threading
    Semaphore when Redis is unavailable or the redis-py library is not
    installed.
    """

    _REDIS_KEY = "ts_harness:runner:active"

    def __init__(self, limit: int, redis_url: str | None = None) -> None:
        self.limit = limit
        self._sem = threading.Semaphore(limit)
        self._redis: Any = None
        if redis_url:
            try:
                import redis  # type: ignore[import]
                self._redis = redis.from_url(redis_url, socket_timeout=2)
                self._redis.ping()
            except Exception:
                self._redis = None

    def try_acquire(self) -> bool:
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.incr(self._REDIS_KEY)
                pipe.expire(self._REDIS_KEY, 3600)
                count, _ = pipe.execute()
                if count > self.limit:
                    self._redis.decr(self._REDIS_KEY)
                    return False
                return True
            except Exception:
                pass
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        if self._redis is not None:
            try:
                self._redis.decr(self._REDIS_KEY)
                return
            except Exception:
                pass
        try:
            self._sem.release()
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Leader election
# ---------------------------------------------------------------------------

class LeaderElection:
    """One leader at a time.

    A Redis URL uses SETNX and fails closed on any Redis error. Otherwise the
    memory API holds a Postgres lease. A missing or failing lease also fails
    closed, so a second runner idles instead of dispatching.
    """

    LOCK_KEY = "TS_HARNESS_RUNNER_LEADER"

    def __init__(
        self,
        runner_id: str,
        redis_url: str | None = None,
        ttl_seconds: int = 30,
        memory: Any = None,
    ) -> None:
        self.runner_id = runner_id
        self.ttl = ttl_seconds
        self.memory = memory
        self._redis: Any = None
        self._redis_required = bool(redis_url)
        if redis_url:
            try:
                import redis  # type: ignore[import]
                self._redis = redis.from_url(redis_url, socket_timeout=2)
                self._redis.ping()
            except Exception:
                self._redis = None

    def try_become_leader(self) -> bool:
        if self._redis_required:
            if self._redis is None:
                return False
            try:
                acquired = self._redis.set(
                    self.LOCK_KEY, self.runner_id, nx=True, ex=self.ttl
                )
                if acquired:
                    return True
                current = self._redis.get(self.LOCK_KEY)
                if current and current.decode() == self.runner_id:
                    self._redis.expire(self.LOCK_KEY, self.ttl)
                    return True
                return False
            except Exception:
                return False
        if self.memory is None:
            return False
        try:
            return bool(self.memory.acquire_leader(self.runner_id, self.ttl))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Memory-api client
# ---------------------------------------------------------------------------

class MemoryApiClient:
    def __init__(self, base_url: str = DEFAULT_MEMORY_API_URL, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else os.environ.get("MEMORY_API_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        raw = _http_request_with_retries(
            method,
            f"{self.base_url}{path}",
            headers=self._headers(),
            body=data,
            timeout=30,
        )
        return json.loads(raw) if raw else {}

    def acquire_leader(self, runner_id: str, ttl_seconds: int) -> bool:
        result = self._request(
            "POST",
            "/runner/leader",
            {"runner_id": runner_id, "ttl_seconds": ttl_seconds},
        )
        return bool(result.get("leader"))

    def list_stale_tasks(self) -> list[dict[str, Any]]:
        try:
            return list(self._request("POST", "/workflow/stale", {"limit": 50}).get("tasks", []))
        except RuntimeError as exc:
            log_event("memory_api_unavailable", path="/workflow/stale", error=str(exc))
            return []

    def list_ready_tasks(self, limit: int) -> list[dict[str, Any]]:
        try:
            return list(self._request("POST", "/workflow/ready", {"limit": limit}).get("tasks", []))
        except RuntimeError as exc:
            log_event("memory_api_unavailable", path="/workflow/ready", error=str(exc))
            return []

    def claim_task(self, task: dict[str, Any], locked_by: str, lease_seconds: int) -> bool:
        result = self._request(
            "POST",
            "/workflow/claim",
            {
                "id": task["id"],
                "agent_id": task["agent_id"],
                "locked_by": locked_by,
                "lease_seconds": lease_seconds,
                "actor": "workflow-runner",
            },
        )
        return bool(result.get("claimed"))

    def record_runtime_task(self, task_id: str, runtime_task_id: str) -> None:
        self._request(
            "POST",
            "/workflow/runtime",
            {"id": task_id, "runtime_task_id": runtime_task_id, "actor": "workflow-runner"},
        )

    def complete_task(self, task_id: str, output: dict[str, Any]) -> None:
        self._request("POST", "/workflow/complete", {"id": task_id, "output": output})

    def fail_task(self, task_id: str, error: str) -> None:
        self._request("POST", "/workflow/fail", {"id": task_id, "error": error})

    def block_task(
        self,
        task_id: str,
        error: str,
        output: dict[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {"id": task_id, "error": error, "actor": "workflow-runner"}
        if output is not None:
            body["output"] = output
        self._request("POST", "/workflow/block", body)

    def set_task_wait(
        self,
        task_id: str,
        *,
        wait_token: str,
        expected_event_type: str,
        escalate_after_seconds: int | None = None,
        expires_after_seconds: int | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "id": task_id,
            "wait_token": wait_token,
            "expected_event_type": expected_event_type,
            "actor": "workflow-runner",
        }
        if escalate_after_seconds:
            body["escalate_after_seconds"] = escalate_after_seconds
        if expires_after_seconds:
            body["expires_after_seconds"] = expires_after_seconds
        self._request("POST", "/workflow/task/wait", body)

    def retry_task(self, task_id: str, reason: str) -> None:
        self._request(
            "POST",
            "/workflow/retry",
            {"id": task_id, "reason": reason, "actor": "workflow-runner"},
        )

    def propose_plan(
        self, workflow_id: str, parent_task_id: str, proposed_tasks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/workflow/plan/propose",
            {
                "workflow_id": workflow_id,
                "parent_task_id": parent_task_id,
                "proposed_tasks": proposed_tasks,
                "actor": "workflow-runner",
            },
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflow/plan/{plan_id}")

    def create_workflow(
        self,
        workflow_id: str,
        workflow_type: str,
        tenant_id: str,
        definition_version: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "id": workflow_id,
            "workflow_type": workflow_type,
            "tenant_id": tenant_id,
            "actor": "workflow-runner",
        }
        if definition_version is not None:
            body["definition_version"] = definition_version
        return self._request(
            "POST",
            "/workflows/create",
            body,
        )

    def get_workflow_record(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{workflow_id}")

    def task_program(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflow/tasks/{urllib.parse.quote(str(task_id), safe='')}/program")

    def list_coding_jobs(
        self,
        *,
        task_id: str | None = None,
        workflow_id: str | None = None,
        status: str | None = None,
        unconsumed: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (("task_id", task_id), ("workflow_id", workflow_id), ("status", status)):
            if value:
                params[key] = value
        if unconsumed:
            params["unconsumed"] = "true"
        return list(self._request("GET", f"/coding-jobs?{urllib.parse.urlencode(params)}").get("jobs") or [])

    def create_coding_job(self, **body: Any) -> dict[str, Any]:
        return self._request("POST", "/coding-jobs/create", {**body, "actor": "workflow-runner"})

    def consume_coding_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/coding-jobs/{job_id}/consume", {})


# ---------------------------------------------------------------------------
# Gateway adapter
# ---------------------------------------------------------------------------

class GatewayHttpAdapter:
    def __init__(self, gateway_url: str, token: str, timeout_seconds: float | None = None):
        if not gateway_url:
            raise RuntimeError("AGENT_RUNTIME_URL is required for gateway-http adapter")
        if not token:
            raise RuntimeError("GATEWAY_TOKEN is required for gateway-http adapter")
        self.gateway_url = gateway_url.rstrip("/")
        self.token = token
        # Coding deliver stages can run many minutes; keep above stage timeout.
        if timeout_seconds is None:
            timeout_seconds = float(os.environ.get("WORKFLOW_RUNNER_DISPATCH_TIMEOUT_SECONDS", "7200"))
        self.timeout_seconds = max(60.0, float(timeout_seconds))

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        try:
            from ts_harness.observability.logging import current_trace_id, traceparent_header

            trace_id = current_trace_id()
            if trace_id:
                headers["traceparent"] = traceparent_header(trace_id)
        except Exception:
            pass
        request = urllib.request.Request(
            f"{self.gateway_url}/workflow/dispatch",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode() or str(exc)
            if 500 <= int(exc.code) < 600:
                return {"status": "blocked", "error": f"{TRANSPORT_ERROR_PREFIX} {detail}"}
            return {"status": "blocked", "error": detail}
        except urllib.error.URLError as exc:
            detail = str(exc.reason if getattr(exc, "reason", None) else exc)
            return {"status": "blocked", "error": f"{TRANSPORT_ERROR_PREFIX} {detail}"}
        except TimeoutError as exc:
            return {"status": "blocked", "error": f"{TRANSPORT_ERROR_PREFIX} {exc}"}
        return json.loads(raw) if raw else {"status": "blocked", "error": "empty response"}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class WorkflowRunner:
    def __init__(
        self,
        memory: Any,
        adapter: Any,
        runner_id: str = "workflow-runner",
        batch_size: int = 5,
        lease_seconds: int = 1800,
        stale_policy: str = "block",
        tick_file: Path = DEFAULT_TICK_FILE,
        skeletons: dict[str, workflow_definitions.WorkflowSkeleton] | None = None,
        skeleton_dir: str | Path | None = None,
        concurrency_limiter: ConcurrencyLimiter | None = None,
        leader_election: LeaderElection | None = None,
        runtime_url: str | None = None,
        stop_requested: threading.Event | None = None,
        executor: Executor | None = None,
    ):
        if stale_policy not in {"block", "fail"}:
            raise ValueError("stale_policy must be 'block' or 'fail'")
        self.memory = memory
        self.adapter = adapter
        self.runner_id = runner_id
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.stale_policy = stale_policy
        self.tick_file = tick_file
        self.skeletons = skeletons or {}
        self._versioned_skeletons: dict[
            tuple[str, str],
            workflow_definitions.WorkflowSkeleton,
        ] = dict(_loaded_versioned_skeletons)
        self.skeleton_dir = Path(skeleton_dir) if skeleton_dir is not None else None
        self._definition_manifest_generation = read_definition_manifest_generation()
        self._limiter = concurrency_limiter or ConcurrencyLimiter(batch_size)
        self._leader = leader_election or LeaderElection(runner_id, memory=memory)
        self.stop_requested = stop_requested or threading.Event()
        # Empty/None disables the healthz gate (unit tests); production main() sets this.
        self.runtime_url = (runtime_url or "").rstrip("/")
        # Track workflow_ids we've already created records for this process lifetime.
        self._created_workflows: set[str] = set()
        # None dispatches inline on the tick thread (unit tests); main() passes a pool.
        self._executor = executor
        # Only agent turns run in parallel. Task transitions and hooks hold this lock,
        # so two workers never update workflow state at the same time.
        self._state_lock = threading.Lock()
        self._inflight_lock = threading.Lock()
        self._inflight: dict[str, str] = {}

    def tick(self) -> None:
        try:
            if not self._leader.try_become_leader():
                log_event("leader_election_lost", runner_id=self.runner_id)
                return

            log_event("ticked", runner_id=self.runner_id)
            with self._state_lock:
                self.reload_if_changed()
                self._sweep_stale()
            if not self._runtime_ready():
                log_event("runtime_not_ready", runner_id=self.runner_id)
                return
            for task in self.memory.list_ready_tasks(self.batch_size):
                if self.stop_requested.is_set():
                    log_event("runner_stop_requested", runner_id=self.runner_id)
                    break
                if self._should_skip_retry_backoff(task):
                    continue
                workflow_id = str(task.get("workflow_id") or "")
                if workflow_id and self._workflow_in_flight(workflow_id):
                    continue
                if not self._limiter.try_acquire():
                    log_event("concurrency_limit_reached", runner_id=self.runner_id)
                    break
                if not self.memory.claim_task(task, self.runner_id, self.lease_seconds):
                    self._limiter.release()
                    continue
                with self._inflight_lock:
                    self._inflight[str(task.get("id") or "")] = workflow_id
                if self._executor is None:
                    self._run_claimed_task(task)
                else:
                    self._executor.submit(contextvars.copy_context().run, self._run_claimed_task, task)
        finally:
            self._touch_tick_file()

    def _workflow_in_flight(self, workflow_id: str) -> bool:
        with self._inflight_lock:
            return workflow_id in self._inflight.values()

    def _task_in_flight(self, task_id: str) -> bool:
        with self._inflight_lock:
            return task_id in self._inflight

    def _run_claimed_task(self, task: dict[str, Any]) -> None:
        from ts_harness.observability.logging import new_trace_id, trace_id_var

        trace_token = trace_id_var.set(new_trace_id())
        try:
            log_event(
                "claimed",
                workflow_id=task.get("workflow_id"),
                task_id=task.get("id"),
                agent_id=task.get("agent_id"),
            )
            _runner_metrics.inc("runner_tasks_claimed")
            with self._state_lock:
                try:
                    self._ensure_workflow_record(task)
                    self._dispatch_task(task)
                except Exception as exc:
                    log_event(
                        "dispatch_exception",
                        workflow_id=task.get("workflow_id"),
                        task_id=task.get("id"),
                        error=str(exc),
                    )
                    self._recover_claimed_task(task, str(exc))
        finally:
            with self._inflight_lock:
                self._inflight.pop(str(task.get("id") or ""), None)
            self._limiter.release()
            trace_id_var.reset(trace_token)

    def _dispatch_unlocked(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the agent turn without the state lock so other workers can record results."""
        self._state_lock.release()
        try:
            return self.adapter.dispatch(payload)
        finally:
            self._state_lock.acquire()

    def shutdown(self) -> None:
        if self._executor is None:
            return
        with self._inflight_lock:
            inflight = len(self._inflight)
        log_event("runner_draining", runner_id=self.runner_id, inflight=inflight)
        # In-flight agent turns keep running in agent-runtime; their tasks are swept
        # once the lease expires, as after any runner restart.
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _runtime_ready(self) -> bool:
        """Return True when agent-runtime healthz is reachable, or gate is disabled."""
        if not self.runtime_url:
            return True
        try:
            _http_request_with_retries(
                "GET",
                f"{self.runtime_url}/healthz",
                headers={},
                body=None,
                timeout=2,
                max_attempts=1,
            )
            return True
        except Exception as exc:
            log_event(
                "runtime_healthz_failed",
                runner_id=self.runner_id,
                runtime_url=self.runtime_url,
                error=str(exc),
            )
            return False

    def reload_definitions(self) -> bool:
        if self.skeleton_dir is None:
            return False
        try:
            skeletons = load_workflow_skeletons(self.skeleton_dir)
            generation = read_definition_manifest_generation()
        except Exception as exc:
            log_event("definition_reload_failed", error=str(exc))
            return False
        for workflow_type, skeleton in self.skeletons.items():
            version = str(getattr(skeleton, "definition_version", "") or "").strip()
            if version:
                self._versioned_skeletons.setdefault((workflow_type, version), skeleton)
        self.skeletons = skeletons
        self._definition_manifest_generation = generation
        log_event("definition_reload_succeeded", generation=generation)
        return True

    def reload_if_changed(self) -> bool:
        try:
            generation = read_definition_manifest_generation()
        except Exception as exc:
            log_event("definition_manifest_read_failed", error=str(exc))
            return False
        if generation == self._definition_manifest_generation:
            return True
        return self.reload_definitions()

    def _should_retry_task(self, task: dict[str, Any]) -> bool:
        return int(task.get("attempt", 0)) < int(task.get("max_retries", 0))

    def _recover_claimed_task(self, task: dict[str, Any], error: str) -> None:
        """Block a claimed task after an unexpected failure and requeue when budget remains."""
        if not self._transition_task_or_log_conflict(
            task,
            event="blocked",
            error=error,
        ):
            return
        if self._should_retry_task(task):
            try:
                self.memory.retry_task(task["id"], error)
                log_event(
                    "retry_scheduled",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    reason=error,
                )
            except Exception as retry_exc:
                log_event(
                    "retry_schedule_error",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    error=str(retry_exc),
                )

    def _should_skip_retry_backoff(self, task: dict[str, Any]) -> bool:
        """Return True if this task's retry window has not yet elapsed."""
        next_attempt_at = task.get("next_attempt_at")
        if not next_attempt_at:
            return False
        try:
            # Accept ISO-8601 with or without trailing Z.
            import datetime
            ts_str = str(next_attempt_at).rstrip("Z")
            next_dt = datetime.datetime.fromisoformat(ts_str).replace(
                tzinfo=datetime.timezone.utc
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            if next_dt > now:
                log_event(
                    "retry_backoff_skipped",
                    task_id=task.get("id"),
                    next_attempt_at=next_attempt_at,
                )
                return True
        except Exception:
            pass
        return False

    def _ensure_workflow_record(self, task: dict[str, Any]) -> None:
        """Create a workflow record the first time we see a workflow_id."""
        workflow_id = task.get("workflow_id")
        if not workflow_id or workflow_id in self._created_workflows:
            return
        try:
            definition_version = task.get("definition_version")
            if not definition_version:
                skeleton = self.skeletons.get(str(task.get("workflow_type") or ""))
                if skeleton is not None:
                    definition_version = str(skeleton.definition_version)
            self.memory.create_workflow(
                workflow_id=workflow_id,
                workflow_type=task.get("workflow_type", "generic"),
                tenant_id=task.get("tenant_id", ""),
                definition_version=definition_version,
            )
        except Exception as exc:
            # Record creation may fail if the workflow already exists; log and continue.
            log_event("workflow_record_create_error", workflow_id=workflow_id, error=str(exc))
        self._created_workflows.add(workflow_id)

    def _sweep_stale(self) -> None:
        for task in self.memory.list_stale_tasks():
            task_id = task["id"]
            # A long agent turn outlives its lease while ticks continue; only
            # tasks no worker here is running are abandoned.
            if self._task_in_flight(str(task_id)):
                continue
            if self.stale_policy == "fail":
                self.memory.fail_task(task_id, "task lease expired")
                event = "failed"
            else:
                self.memory.block_task(task_id, "task lease expired")
                event = "stale_swept"
            _runner_metrics.inc("runner_tasks_stale")
            log_event(event, task_id=task_id, reason="task lease expired")

    def _notify_bound_thread(
        self,
        task: dict[str, Any],
        *,
        event: str,
        error: str | None = None,
    ) -> None:
        workflow_id = task.get("workflow_id")
        if not workflow_id:
            return
        try:
            from ts_harness.notifications.slack import SlackNotifier

            record = self.memory.get_workflow_record(str(workflow_id))
            source_channel = str((record.get("workflow") or {}).get("source_channel") or "")
            if source_channel.count(":") < 2:
                return
            _, channel_id, thread_ts = source_channel.split(":", 2)
            SlackNotifier().notify_workflow_event(
                event=event,
                workflow_id=str(workflow_id),
                task_id=str(task.get("id") or ""),
                agent_id=str(task.get("agent_id") or ""),
                step_name=str(task.get("step_name") or ""),
                tenant_id=str(task.get("tenant_id") or ""),
                error=error,
                channel=channel_id,
                thread_ts=thread_ts,
            )
        except Exception as exc:
            log_event("thread_notify_error", workflow_id=workflow_id, error=str(exc))

    def _transition_task_or_log_conflict(
        self,
        task: dict[str, Any],
        *,
        event: str,
        error: str,
        dispatch_result: dict[str, Any] | None = None,
    ) -> bool:
        try:
            if event == "blocked":
                rejected_output: dict[str, Any] | None = None
                raw_output = (
                    dispatch_result.get("output")
                    if isinstance(dispatch_result, dict)
                    else None
                )
                if isinstance(raw_output, dict) and raw_output:
                    rejected_output = dict(raw_output)
                    rejected_output["rejected"] = True
                    rejected_output["validation_error"] = error
                self.memory.block_task(task["id"], error, output=rejected_output)
            else:
                self.memory.fail_task(task["id"], error)
            return True
        except RuntimeError as exc:
            detail = str(exc)
            if "illegal workflow transition" not in detail:
                raise
            log_event(
                f"{event}_transition_conflict",
                workflow_id=task.get("workflow_id"),
                task_id=task.get("id"),
                error=error,
                detail=detail,
            )
            if "succeeded ->" in detail:
                self._recover_succeeded_transition(
                    task,
                    dispatch_result=dispatch_result or {},
                )
            return False

    def _recover_succeeded_transition(
        self,
        task: dict[str, Any],
        *,
        dispatch_result: dict[str, Any],
    ) -> None:
        workflow_id = str(task.get("workflow_id") or "")
        task_id = str(task.get("id") or "")
        try:
            payload = self.memory.get_workflow_record(workflow_id)
            tasks = list(payload.get("tasks") or [])
            current = next(
                (item for item in tasks if str(item.get("id") or "") == task_id),
                None,
            )
            if current is None:
                return
            output = current.get("output") if isinstance(current, dict) else {}
            recovered_result = {
                **dispatch_result,
                "status": "succeeded",
                "output": output if isinstance(output, dict) else {},
            }
            recovered_task = {**task, **current}
            log_event(
                "succeeded_transition_recovered",
                workflow_id=workflow_id,
                task_id=task_id,
            )
            from ts_harness.workflows.engine import (
                hook_engine_enabled,
            )

            if hook_engine_enabled():
                _workflow_engine().on_task_transition(
                    recovered_task,
                    "succeeded",
                    dispatch_result=recovered_result,
                )
        except Exception as exc:
            log_event(
                "succeeded_transition_recovery_error",
                workflow_id=workflow_id,
                task_id=task_id,
                error=str(exc),
            )

    def _prepare_checks_wait(self, task: dict[str, Any], config: dict[str, Any]) -> None:
        from ts_harness.workflows.merge_readiness import CHECKS_EVENT, prepare_checks_wait

        engine = _workflow_engine()
        prepared = prepare_checks_wait(task, config, memory=engine.client)
        log_event(
            "pr_checks_wait_prepared",
            workflow_id=task.get("workflow_id"),
            task_id=task.get("id"),
            head_sha=prepared["match"].get("head_sha"),
            checks_state=prepared["summary"].get("state"),
            warnings=prepared["options"].get("warnings"),
        )
        self._resume_ready_wait(task, CHECKS_EVENT, prepared.get("ready"))

    def _prepare_release_wait(self, task: dict[str, Any], config: dict[str, Any]) -> None:
        from ts_harness.workflows import release

        engine = _workflow_engine()
        if config.get("sha_checks"):
            event_type = release.CHECKS_EVENT
            prepared = release.prepare_sha_checks_wait(task, config, memory=engine.client)
        else:
            event_type = release.DEPLOYMENT_EVENT
            prepared = release.prepare_deployment_wait(task, config, memory=engine.client)
        log_event(
            "release_wait_prepared",
            workflow_id=task.get("workflow_id"),
            task_id=task.get("id"),
            event_type=event_type,
            match=prepared["match"],
        )
        self._resume_ready_wait(task, event_type, prepared.get("ready"))

    def _prepare_children_wait(self, task: dict[str, Any], config: dict[str, Any]) -> None:
        from ts_harness.workflows import work_order_waves

        prepared = work_order_waves.prepare_children_wait(task, config, memory=_workflow_engine().client)
        log_event(
            "children_wait_prepared",
            workflow_id=task.get("workflow_id"),
            task_id=task.get("id"),
            children=len(prepared["options"]["children"]),
        )
        self._resume_ready_wait(task, work_order_waves.CHILD_EVENT, prepared.get("ready"))

    def _prepare_upstream_wait(self, task: dict[str, Any], config: dict[str, Any]) -> None:
        from ts_harness.workflows import release_tracks

        prepared = release_tracks.prepare_upstream_wait(task, config, memory=_workflow_engine().client)
        log_event(
            "upstream_wait_prepared",
            workflow_id=task.get("workflow_id"),
            task_id=task.get("id"),
            pending=prepared["options"]["pending"],
        )
        self._resume_ready_wait(task, release_tracks.UPSTREAM_EVENT, prepared.get("ready"))

    def _resume_ready_wait(self, task: dict[str, Any], event_type: str, ready: dict[str, Any] | None) -> None:
        if not ready:
            return
        _workflow_engine().resume_external(
            str(task["workflow_id"]),
            event_type=event_type,
            payload=ready,
            actor="workflow-runner",
            tenant_id=str(task.get("tenant_id") or "default"),
            task_output={**ready, "event_type": event_type, "external_event": ready},
        )

    def _dispatch_task(self, task: dict[str, Any]) -> None:
        dispatch_started = time.time()
        stage = self._stage_for_task(task)

        from ts_harness.workflows import human_decisions

        if human_decisions.accepted(task):
            self._complete_succeeded(task, human_decisions.accepted_output(task), event="human_accepted")
            return

        if stage is not None and stage.barrier:
            self._handle_barrier(task)
            return

        if stage is not None and stage.wait_external:
            event_type = str(stage.wait_external.get("event_type") or "external.event")
            try:
                wait_token = str(uuid.uuid4())
                self.memory.set_task_wait(
                    task["id"],
                    wait_token=wait_token,
                    expected_event_type=event_type,
                    **wait_deadlines(stage.wait_external),
                )
                self.memory.block_task(task["id"], f"awaiting_external:{event_type}")
                log_event(
                    "awaiting_external",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    event_type=event_type,
                )
                if stage.wait_external.get("pr_checks"):
                    self._prepare_checks_wait(task, stage.wait_external)
                elif stage.wait_external.get("sha_checks") or stage.wait_external.get("deployment"):
                    self._prepare_release_wait(task, stage.wait_external)
                elif stage.wait_external.get("children"):
                    self._prepare_children_wait(task, stage.wait_external)
                elif stage.wait_external.get("upstream"):
                    self._prepare_upstream_wait(task, stage.wait_external)
            except Exception as exc:
                from ts_harness.integrations.github_client import classify_github_wait_error

                retryable, message = classify_github_wait_error(exc)
                error = f"wait_external setup failed: {message}"
                self.memory.block_task(task["id"], error)
                if retryable and self._should_retry_task(task):
                    self.memory.retry_task(task["id"], error)
                    log_event("wait_external_retry", task_id=task.get("id"), error=message)
                else:
                    log_event("wait_external_error", task_id=task.get("id"), error=message)
            return

        # Plan approval gating: for expand stages that require plan approval,
        # check whether the outstanding plan has been approved before proceeding.
        if stage is not None and stage.plan_approval_required:
            plan_id = task.get("plan_id")
            if plan_id:
                try:
                    plan = self.memory.get_plan(plan_id)
                    if plan.get("status") != "approved":
                        log_event(
                            "plan_awaiting_approval",
                            workflow_id=task.get("workflow_id"),
                            task_id=task.get("id"),
                            plan_id=plan_id,
                        )
                        # Release claim silently so the task stays ready for the next tick.
                        return
                except Exception as exc:
                    log_event("plan_fetch_error", task_id=task.get("id"), error=str(exc))

        contract = self._contract_for_task(task, stage)
        task_input = dict(task.get("input") or {})

        from ts_harness.workflows import coding_job_stage

        if stage is not None and stage.execution == "coding_job" and coding_job_stage.enabled():
            self._dispatch_coding_job(task, stage, task_input, contract, dispatch_started)
            return

        if task.get("step_name") == "qa_gate" and not task_input.get("files"):
            try:
                record = self.memory.get_workflow_record(str(task.get("workflow_id") or ""))
                task_input.update(qa_review_evidence(task, list(record.get("tasks") or [])))
            except Exception as exc:
                log_event("qa_evidence_error", task_id=task.get("id"), error=str(exc))
        payload = {
            "workflow_id": task.get("workflow_id"),
            "task_id": task.get("id"),
            "workflow_type": task.get("workflow_type", "generic"),
            "step_name": task.get("step_name"),
            "agent_id": task.get("agent_id"),
            "tenant_id": task.get("tenant_id", ""),
            "input": task_input,
            "required_artifacts": task.get("required_artifacts")
            or ([] if stage is None else stage.required_artifacts),
            "skill_hints": task.get("skill_hints")
            or ([] if stage is None else stage.skill_hints),
            "contract": contract or {},
            "definition_version": task.get("definition_version"),
        }
        if stage is not None and stage.tool_allow is not None:
            payload["tool_allow"] = list(stage.tool_allow)
        is_qa = task.get("step_name") == "qa_gate"
        if is_qa:
            self._publish_qa_status(task, "qa_started", task_input)
        result = self._dispatch_unlocked(payload)
        log_event("dispatched", workflow_id=payload["workflow_id"], task_id=payload["task_id"])
        if is_qa:
            self._publish_qa_status(task, "qa_finished", task_input, result)
        self._apply_dispatch_result(task, stage, result, dispatch_started)

    def _dispatch_coding_job(
        self,
        task: dict[str, Any],
        stage: workflow_definitions.Stage,
        task_input: dict[str, Any],
        contract: dict[str, Any] | None,
        dispatch_started: float,
    ) -> None:
        """Use the task's finished coding job, or queue one and park the task until it ends."""
        from ts_harness.implementation.coding_job import CodingRequestError
        from ts_harness.workflows import coding_job_stage

        task_id = str(task["id"])
        jobs = self.memory.list_coding_jobs(task_id=task_id, unconsumed=True, limit=10)
        finished = next((j for j in jobs if j.get("status") in coding_job_stage.TERMINAL), None)
        if finished is not None:
            result = coding_job_stage.dispatch_result(finished, contract, task_input)
            job_id = result.pop("coding_job_id", "")
            log_event("coding_job_result", workflow_id=task.get("workflow_id"), task_id=task_id, job_id=job_id, status=result["status"])
            try:
                self._apply_dispatch_result(task, stage, result, dispatch_started)
            finally:
                try:
                    self.memory.consume_coding_job(job_id)
                except Exception as exc:
                    log_event("coding_job_consume_error", task_id=task_id, job_id=job_id, error=str(exc))
            return

        job = next((j for j in jobs if j.get("status") in coding_job_stage.ACTIVE), None)
        if job is None:
            try:
                program_id = (self.memory.task_program(task_id) or {}).get("program_id")
            except Exception:
                program_id = None
            try:
                spec = coding_job_stage.stage_spec(task, task_input, program_id=program_id)
            except CodingRequestError as exc:
                self._apply_dispatch_result(task, stage, {"status": "blocked", "error": str(exc), "output": {}}, dispatch_started)
                return
            job = self.memory.create_coding_job(
                spec=spec,
                workflow_id=str(task.get("workflow_id") or "") or None,
                task_id=task_id,
                program_id=program_id,
                tenant_id=str(task.get("tenant_id") or "default"),
            )
        self.memory.set_task_wait(
            task_id,
            wait_token=str(job["id"]),
            expected_event_type=coding_job_stage.EVENT_TYPE,
        )
        self.memory.block_task(task_id, coding_job_stage.WAIT_ERROR)
        log_event("coding_job_queued", workflow_id=task.get("workflow_id"), task_id=task_id, job_id=job["id"])

    def _apply_dispatch_result(
        self,
        task: dict[str, Any],
        stage: workflow_definitions.Stage | None,
        result: dict[str, Any],
        dispatch_started: float,
    ) -> None:
        runtime_task_id = result.get("runtime_task_id")
        if runtime_task_id:
            self.memory.record_runtime_task(task["id"], runtime_task_id)
            log_event(
                "runtime_recorded",
                workflow_id=task.get("workflow_id"),
                task_id=task.get("id"),
                runtime_task_id=runtime_task_id,
            )

        status = result.get("status")
        if status == "succeeded":
            _runner_metrics.inc("runner_tasks_succeeded")
            _runner_metrics.observe("runner_dispatch_latency_ms", (time.time() - dispatch_started) * 1000)
            output = dict(result.get("output") or {})
            hook_result = None
            try:
                from ts_harness.workflows.engine import hook_engine_enabled

                if hook_engine_enabled():
                    hook_result = _workflow_engine().on_task_transition(
                        task,
                        "succeeded",
                        dispatch_result=result,
                    )
            except Exception as exc:
                error = f"hook_transition_error: {exc}"
                log_event("hook_transition_error", task_id=task.get("id"), error=str(exc))
                self.memory.fail_task(task["id"], error)
                return

            if hook_result and hook_result.get("status") == "awaiting_plan_approval":
                log_event(
                    "awaiting_plan_approval",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    plan_id=hook_result.get("plan_id"),
                )
                return

            if (
                hook_result
                and hook_result.get("hook") == "present_specialist_output"
                and hook_result.get("status") not in {"presented", "skipped"}
            ):
                error = str(
                    hook_result.get("error")
                    or hook_result.get("reason")
                    or "specialist output presentation failed"
                )
                self.memory.block_task(task["id"], error)
                log_event(
                    "presentation_blocked",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    error=error,
                )
                return

            proposed_tasks = result.get("proposed_tasks")
            if proposed_tasks and hook_result is None:
                if stage is None or stage.kind != "expand":
                    self.memory.fail_task(
                        task["id"],
                        "proposed_tasks returned for non-expand stage",
                    )
                    return
                try:
                    skeleton = self._skeleton_for_task(task)
                    if skeleton is None:
                        self.memory.fail_task(
                            task["id"],
                            "workflow skeleton not found for proposed tasks",
                        )
                        return
                    existing_step_names: set[str] = set()
                    try:
                        record = self.memory.get_workflow_record(str(task["workflow_id"]))
                        for item in list(record.get("tasks") or []):
                            step_name = str(item.get("step_name") or "").strip()
                            if step_name:
                                existing_step_names.add(step_name)
                    except Exception as exc:
                        log_event(
                            "expand_existing_steps_lookup_failed",
                            workflow_id=task.get("workflow_id"),
                            error=str(exc),
                        )
                    proposed_tasks = workflow_definitions.validate_proposed_tasks(
                        skeleton,
                        stage,
                        proposed_tasks,
                        existing_step_names=existing_step_names,
                    )
                except ValueError as exc:
                    self.memory.fail_task(task["id"], str(exc))
                    return
                plan = self.memory.propose_plan(
                    task["workflow_id"], task["id"], proposed_tasks
                )
                output["plan_id"] = plan.get("plan_id")
                log_event(
                    "plan_proposed",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    plan_id=plan.get("plan_id"),
                )
                if stage.plan_approval_required:
                    self.memory.block_task(
                        task["id"],
                        f"awaiting_plan_approval:{plan.get('plan_id', '')}",
                    )
                    log_event(
                        "awaiting_plan_approval",
                        workflow_id=task.get("workflow_id"),
                        task_id=task.get("id"),
                        plan_id=plan.get("plan_id"),
                    )
                    return
            elif hook_result:
                if hook_result.get("output"):
                    output = dict(hook_result["output"])
                elif hook_result.get("plan_id"):
                    output["plan_id"] = hook_result["plan_id"]

            self.memory.complete_task(task["id"], output)
            log_event("completed", workflow_id=task.get("workflow_id"), task_id=task.get("id"))
            # complete_program_phase / auto-advance need the task persisted as
            # succeeded; on_task_transition runs before complete_task and skips.
            try:
                from ts_harness.workflows.engine import (
                    hook_engine_enabled,
                )

                if hook_engine_enabled():
                    persisted_result = {**result, "output": output, "status": "succeeded"}
                    phase_result = _workflow_engine().after_task_succeeded(
                        {**task, "status": "succeeded", "output": output},
                        dispatch_result=persisted_result,
                    )
                    if phase_result:
                        log_event(
                            "program_phase_hook",
                            workflow_id=task.get("workflow_id"),
                            task_id=task.get("id"),
                            status=phase_result.get("status"),
                        )
            except Exception as exc:
                log_event(
                    "program_phase_hook_error",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    error=str(exc),
                )
        elif status == "blocked":
            error = str(result.get("error", "dispatch blocked"))
            if str(task.get("step_name") or "") == "qa_gate":
                _runner_metrics.inc("runner_qa_gate_failed")
            if not self._transition_task_or_log_conflict(
                task,
                event="blocked",
                error=error,
                dispatch_result=result,
            ):
                return
            log_event(
                "blocked",
                workflow_id=task.get("workflow_id"),
                task_id=task.get("id"),
                error=error,
            )
            if error.startswith(AWAITING_TOOL_APPROVAL_PREFIX):
                log_event(
                    "awaiting_tool_approval",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    approval_id=error[len(AWAITING_TOOL_APPROVAL_PREFIX):],
                )
                output = result.get("output") if isinstance(result.get("output"), dict) else {}
                try:
                    _workflow_engine().notify_tool_approval_waiting(
                        task, tool_name=str(output.get("tool_name") or "")
                    )
                except Exception as exc:
                    log_event(
                        "tool_approval_notify_error",
                        workflow_id=task.get("workflow_id"),
                        task_id=task.get("id"),
                        error=str(exc),
                    )
                return
            should_retry = self._should_retry_task(task) and (
                error.startswith(TRANSPORT_ERROR_PREFIX)
                or "output must include artifacts" in error
            )
            if should_retry:
                self.memory.retry_task(task["id"], error)
                log_event(
                    "retry_scheduled",
                    workflow_id=task.get("workflow_id"),
                    task_id=task.get("id"),
                    reason=error,
                )
            else:
                # Terminal block: tell the bound Slack thread instead of going silent.
                try:
                    from ts_harness.workflows.engine import (
                        hook_engine_enabled,
                    )

                    if hook_engine_enabled():
                        notify_result = _workflow_engine().on_task_transition(
                            task,
                            "blocked",
                            error=error,
                            dispatch_result=result,
                        )
                        log_event(
                            "terminal_block_notified",
                            workflow_id=task.get("workflow_id"),
                            task_id=task.get("id"),
                            notify_status=(notify_result or {}).get("status"),
                            error=error,
                        )
                except Exception as exc:
                    log_event(
                        "terminal_block_notify_error",
                        workflow_id=task.get("workflow_id"),
                        task_id=task.get("id"),
                        error=str(exc),
                    )
        else:
            error = str(result.get("error", "dispatch failed"))
            _runner_metrics.inc("runner_tasks_failed")
            _runner_metrics.observe("runner_dispatch_latency_ms", (time.time() - dispatch_started) * 1000)
            if not self._transition_task_or_log_conflict(
                task,
                event="failed",
                error=error,
                dispatch_result=result,
            ):
                return
            if self._should_retry_task(task):
                self.memory.retry_task(task["id"], "dispatch failed")
            log_event("failed", workflow_id=task.get("workflow_id"), task_id=task.get("id"))
            try:
                from ts_harness.workflows.engine import hook_engine_enabled

                if hook_engine_enabled():
                    _workflow_engine().on_task_transition(task, "failed", error=error)
            except Exception:
                pass

    def _run_barrier_step(self, task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        from ts_harness.workflows import system_steps
        from ts_harness.workflows.hooks import collect_delivery_results as cdr

        hook = str(config.get("hook") or "collect_delivery_results")
        if hook in system_steps.SYSTEM_STEPS:
            return system_steps.run_system_step(hook, task, config, memory=_workflow_engine().client)
        payload = self.memory.get_workflow_record(str(task.get("workflow_id") or ""))
        tasks = list(payload.get("tasks") or [])
        barrier_task = next(
            (item for item in tasks if str(item.get("id")) == str(task.get("id"))),
            task,
        )
        return cdr.aggregate_delivery_results(barrier_task, tasks)

    def _handle_barrier(self, task: dict[str, Any]) -> None:
        """Run a deterministic system step: no agent dispatch.

        ``collect_delivery_results`` (the default) aggregates delivery children;
        the release hooks plan, trigger and record deployments. Each either
        completes with output or blocks the task with a reason.
        """
        stage = self._stage_for_task(task)
        agg = self._run_barrier_step(task, dict(stage.barrier if stage is not None else {}))
        if agg.get("status") != "succeeded":
            reason = str(agg.get("reason") or "delivery barrier blocked")
            blocked_output = dict(agg.get("output") or {}) or None
            error = f"barrier_blocked: {reason}"
            self.memory.block_task(task["id"], error, output=blocked_output)
            log_event(
                "barrier_blocked",
                workflow_id=task.get("workflow_id"),
                task_id=task.get("id"),
                reason=reason,
            )
            try:
                from ts_harness.workflows.engine import hook_engine_enabled

                if hook_engine_enabled():
                    _workflow_engine().on_task_transition(
                        {**task, "status": "blocked", "output": blocked_output or {}},
                        "blocked",
                        dispatch_result={"output": blocked_output or {}},
                        error=error,
                    )
            except Exception as exc:
                log_event("barrier_block_hook_error", task_id=task.get("id"), error=str(exc))
            return

        self._complete_succeeded(task, dict(agg.get("output") or {}), event="barrier_completed")

    def _publish_qa_status(self, task: dict[str, Any], action: str, *args: Any) -> None:
        from ts_harness.workflows import qa_status

        try:
            warnings = getattr(qa_status, action)(*args)
        except Exception as exc:  # noqa: BLE001 - the status is informational
            warnings = [str(exc)]
        if warnings:
            log_event(
                "qa_status_warning",
                workflow_id=task.get("workflow_id"),
                task_id=task.get("id"),
                warnings=warnings,
            )

    def _complete_succeeded(self, task: dict[str, Any], output: dict[str, Any], *, event: str) -> None:
        """Complete a step the runner settled itself (system step or human accept) and run its hooks."""
        result = {"status": "succeeded", "output": output}
        try:
            from ts_harness.workflows.engine import (
                hook_engine_enabled,
            )

            if hook_engine_enabled():
                _workflow_engine().on_task_transition(
                    task, "succeeded", dispatch_result=result
                )
        except Exception as exc:
            log_event("barrier_hook_error", task_id=task.get("id"), error=str(exc))

        self.memory.complete_task(task["id"], output)
        log_event(
            event,
            workflow_id=task.get("workflow_id"),
            task_id=task.get("id"),
            pr_count=len(output.get("pr_urls") or []),
        )
        try:
            from ts_harness.workflows.engine import (
                hook_engine_enabled,
            )

            if hook_engine_enabled():
                _workflow_engine().after_task_succeeded(
                    {**task, "status": "succeeded", "output": output},
                    dispatch_result={**result, "output": output},
                )
        except Exception as exc:
            log_event(
                "program_phase_hook_error",
                workflow_id=task.get("workflow_id"),
                task_id=task.get("id"),
                error=str(exc),
            )

    def _contract_for_task(
        self,
        task: dict[str, Any],
        stage: workflow_definitions.Stage | None,
    ) -> dict[str, Any] | None:
        """Prefer the immutable task.contract snapshot over the live skeleton.

        In-flight tasks execute the contract that was materialized with them so a
        bumped skeleton never rewrites an already-scheduled graph. When a task has
        no persisted contract (legacy rows) fall back to resolving the current
        skeleton stage contract.
        """
        contract = task.get("contract")
        if isinstance(contract, dict) and contract:
            return contract
        skeleton = self._skeleton_for_task(task)
        if skeleton is not None and stage is not None:
            try:
                return skeleton.resolve_stage_contract(stage)
            except Exception:
                return None
        return None

    def _stage_for_task(self, task: dict[str, Any]) -> workflow_definitions.Stage | None:
        workflow_type = task.get("workflow_type")
        step_name = task.get("step_name")
        if not workflow_type or not step_name:
            return None
        skeleton = self._skeleton_for_task(task)
        if skeleton is None:
            return None
        try:
            return skeleton.stage(step_name)
        except KeyError:
            return None

    def _skeleton_for_task(
        self,
        task: dict[str, Any],
    ) -> workflow_definitions.WorkflowSkeleton | None:
        workflow_type = str(task.get("workflow_type") or "")
        if not workflow_type:
            return None
        current = self.skeletons.get(workflow_type)
        requested = str(task.get("definition_version") or "").strip()
        if not requested:
            workflow_id = str(task.get("workflow_id") or "").strip()
            if workflow_id:
                try:
                    record = self.memory.get_workflow_record(workflow_id)
                    workflow = record.get("workflow") or {}
                    if isinstance(workflow, dict):
                        requested = str(workflow.get("definition_version") or "").strip()
                except Exception as exc:
                    log_event(
                        "workflow_record_version_lookup_failed",
                        workflow_id=workflow_id,
                        error=str(exc),
                    )
        if not requested:
            return current
        if current is not None and requested == str(current.definition_version):
            return current

        key = (workflow_type, requested)
        if key in self._versioned_skeletons:
            return self._versioned_skeletons[key]

        store_root = os.environ.get(
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE",
            "",
        ).strip()
        if store_root:
            matches = list_history_paths_for_version(
                Path(store_root),
                workflow_type,
                requested,
            )
            if matches:
                skeleton = workflow_definitions.load_skeleton(matches[-1])
                if skeleton.workflow_type != workflow_type:
                    raise RuntimeError(
                        "workflow history definition type mismatch: "
                        f"requested {workflow_type}, "
                        f"loaded {skeleton.workflow_type}"
                    )
                if str(skeleton.definition_version) != requested:
                    raise RuntimeError(
                        "workflow history definition version mismatch: "
                        f"requested {requested}, "
                        f"loaded {skeleton.definition_version}"
                    )
                self._versioned_skeletons[key] = skeleton
                return skeleton

        if not store_root and current is None:
            return None

        if store_root and current is not None and not _has_managed_active(workflow_type):
            try:
                _ensure_history_snapshot(current)
            except Exception as exc:
                log_event(
                    "workflow_builtin_snapshot_failed",
                    workflow_type=workflow_type,
                    error=str(exc),
                )
            matches = list_history_paths_for_version(
                Path(store_root),
                workflow_type,
                requested,
            )
            if matches:
                skeleton = workflow_definitions.load_skeleton(matches[-1])
                if skeleton.workflow_type == workflow_type and str(
                    skeleton.definition_version
                ) == requested:
                    self._versioned_skeletons[key] = skeleton
                    return skeleton

        message = (
            "workflow skeleton version not found: "
            f"workflow_type={workflow_type} definition_version={requested}"
        )
        log_event("workflow_skeleton_version_missing", error=message)
        raise RuntimeError(message)

    def _touch_tick_file(self) -> None:
        self.tick_file.parent.mkdir(parents=True, exist_ok=True)
        self.tick_file.touch()
        _runner_metrics.persist()


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------

def load_workflow_skeletons(directory: str | Path) -> dict[str, workflow_definitions.WorkflowSkeleton]:
    from ts_harness.workflows.materialize import (
        list_skeleton_types,
        load_skeleton,
    )

    root = Path(directory)
    if not root.is_dir():
        raise RuntimeError(f"workflow skeleton directory not found: {root}")
    skeletons: dict[str, workflow_definitions.WorkflowSkeleton] = {}
    for workflow_type in list_skeleton_types(root):
        skeleton = load_skeleton(workflow_type, root)
        skeletons[skeleton.workflow_type] = skeleton
        _ensure_history_snapshot(skeleton)
        _loaded_versioned_skeletons[
            (skeleton.workflow_type, str(skeleton.definition_version))
        ] = skeleton
    if not skeletons:
        raise RuntimeError(f"no workflow skeletons found in {root}")
    return skeletons


def read_definition_manifest_generation() -> int | None:
    store_root = os.environ.get("TS_HARNESS_WORKFLOW_DEFINITION_STORE", "").strip()
    if not store_root:
        return None
    path = Path(store_root) / "active" / "manifest.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("workflow definition manifest must be an object")
    return int(payload.get("generation") or 0)


def build_adapter() -> Any:
    adapter_name = os.environ.get("WORKFLOW_RUNNER_ADAPTER") or "gateway-http"
    if adapter_name == "gateway-http":
        runtime_url = os.environ.get("AGENT_RUNTIME_URL") or "http://127.0.0.1:18789"
        return GatewayHttpAdapter(
            runtime_url,
            os.environ.get("GATEWAY_TOKEN", ""),
            timeout_seconds=float(os.environ.get("WORKFLOW_RUNNER_DISPATCH_TIMEOUT_SECONDS", "7200")),
        )
    raise RuntimeError(f"unsupported WORKFLOW_RUNNER_ADAPTER: {adapter_name}")


def max_concurrent_dispatches(batch_size: int) -> int:
    return max(1, int(os.environ.get("WORKFLOW_RUNNER_MAX_CONCURRENT", str(batch_size))))


def build_concurrency_limiter(batch_size: int) -> ConcurrencyLimiter:
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("TS_HARNESS_REDIS_URL")
    return ConcurrencyLimiter(limit=max_concurrent_dispatches(batch_size), redis_url=redis_url)


def build_leader_election(runner_id: str, memory: Any = None) -> LeaderElection:
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("TS_HARNESS_REDIS_URL")
    ttl = int(os.environ.get("WORKFLOW_RUNNER_LEADER_TTL_SECONDS", "30"))
    return LeaderElection(
        runner_id=runner_id,
        redis_url=redis_url,
        ttl_seconds=ttl,
        memory=memory,
    )


def run_until_stopped(
    runner: WorkflowRunner,
    *,
    interval: int,
    stop_requested: threading.Event,
) -> None:
    """Poll until SIGTERM/SIGINT. Wait is interruptible, so shutdown does not sleep out the interval."""
    base_interval = interval
    failures = 0
    while not stop_requested.is_set():
        try:
            runner.tick()
            failures = 0
            interval = base_interval
        except Exception as exc:
            failures += 1
            interval = compute_poll_backoff(base_interval, failures)
            log_event(
                "tick_error",
                runner_id=runner.runner_id,
                error=str(exc),
                next_interval=interval,
            )
            try:
                runner._touch_tick_file()
            except Exception:
                pass
        if stop_requested.wait(interval):
            break
    runner.shutdown()
    log_event("runner_shutdown", runner_id=runner.runner_id)


def main() -> None:
    if os.environ.get("WORKFLOW_RUNNER_ENABLED", "false").lower() != "true":
        print("workflow-runner disabled", flush=True)
        return
    skeleton_dir = Path(os.environ.get("WORKFLOW_DEFINITIONS_DIR", "/config/workflows"))
    skeletons = load_workflow_skeletons(skeleton_dir)
    runner_id = os.environ.get("WORKFLOW_RUNNER_ID", "workflow-runner")
    batch_size = int(os.environ.get("WORKFLOW_RUNNER_BATCH_SIZE", "5"))
    runtime_url = (
        os.environ.get("AGENT_RUNTIME_URL") or "http://127.0.0.1:18789"
    )
    runner = WorkflowRunner(
        MemoryApiClient(
            os.environ.get("MEMORY_API_URL", DEFAULT_MEMORY_API_URL),
            os.environ.get("MEMORY_API_TOKEN", ""),
        ),
        build_adapter(),
        runner_id=runner_id,
        batch_size=batch_size,
        lease_seconds=int(os.environ.get("WORKFLOW_RUNNER_LEASE_SECONDS", "1800")),
        stale_policy=os.environ.get("WORKFLOW_RUNNER_STALE_POLICY", "block"),
        skeletons=skeletons,
        skeleton_dir=skeleton_dir,
        concurrency_limiter=build_concurrency_limiter(batch_size),
        runtime_url=runtime_url,
        executor=ThreadPoolExecutor(
            max_workers=max_concurrent_dispatches(batch_size),
            thread_name_prefix="dispatch",
        ),
    )
    stop_requested = threading.Event()
    runner.stop_requested = stop_requested
    runner._leader = build_leader_election(runner_id, runner.memory)

    def _request_stop(signum, _frame):
        stop_requested.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    base_interval = int(os.environ.get("WORKFLOW_RUNNER_POLL_INTERVAL_SECONDS", "10"))
    run_until_stopped(runner, interval=base_interval, stop_requested=stop_requested)


if __name__ == "__main__":
    main()
