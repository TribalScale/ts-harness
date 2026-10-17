"""Background skill review queue worker.

The review worker consumes skill-review requests from an in-memory queue and
runs them asynchronously so that the hot-path agent dispatch is never blocked
by catalog reloads or long policy guard evaluations.

Usage (start once during application startup)::

    from ts_harness.skills.review_worker import SkillReviewWorker, get_review_worker

    worker = get_review_worker()
    worker.start()

    # Enqueue a review from any thread:
    worker.enqueue(tenant_id="acme", agent_id="dev-agent", skill_names=["code-review"])

The worker runs a single background daemon thread.  Results are stored in a
bounded result buffer and can be polled or subscribed to via callbacks.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ReviewRequest:
    tenant_id: str
    agent_id: str
    skill_names: list[str]
    correlation_id: str = ""
    submitted_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )


@dataclass
class ReviewResult:
    request: ReviewRequest
    status: str  # "ok" | "degraded" | "error"
    diagnostics: list[str]
    completed_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    error: str | None = None


class SkillReviewWorker:
    """Single-threaded background worker for async skill review jobs.

    Args:
        catalog: A :class:`~ts_harness.skills.catalog.SkillCatalog`
            instance.  When ``None`` the worker still accepts requests but
            returns a stub result (useful for testing without a full catalog).
        config: Runtime config object passed to ``catalog.resolve_for_agent``.
        max_queue: Maximum pending reviews before ``enqueue`` drops new items.
        result_buffer: Maximum completed results kept in memory.
        on_result: Optional callback invoked after each completed review.
    """

    def __init__(
        self,
        catalog: Any | None = None,
        config: Any | None = None,
        *,
        max_queue: int = 256,
        result_buffer: int = 512,
        on_result: Callable[[ReviewResult], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._config = config
        self._queue: queue.Queue[ReviewRequest] = queue.Queue(maxsize=max_queue)
        self._results: list[ReviewResult] = []
        self._result_lock = threading.Lock()
        self._result_buffer = result_buffer
        self._on_result = on_result
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background review thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="skill-review-worker",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and wait up to *timeout* seconds."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        skill_names: list[str],
        correlation_id: str = "",
    ) -> bool:
        """Submit a review request.  Returns False if the queue is full."""
        req = ReviewRequest(
            tenant_id=tenant_id,
            agent_id=agent_id,
            skill_names=list(skill_names),
            correlation_id=correlation_id,
        )
        try:
            self._queue.put_nowait(req)
            return True
        except queue.Full:
            return False

    def recent_results(self, n: int = 20) -> list[ReviewResult]:
        """Return up to *n* most-recent completed results."""
        with self._result_lock:
            return list(self._results[-n:])

    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                req = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            result = self._process(req)
            self._store_result(result)
            if self._on_result is not None:
                try:
                    self._on_result(result)
                except Exception:
                    pass
            self._queue.task_done()

    def _process(self, req: ReviewRequest) -> ReviewResult:
        if self._catalog is None or self._config is None:
            return ReviewResult(
                request=req,
                status="ok",
                diagnostics=["catalog not configured – stub review"],
            )
        try:
            agent_cfg = self._config.get_agent(req.agent_id)
            _skills, diagnostics = self._catalog.resolve_for_agent(
                agent_cfg,
                self._config,
                tenant_id=req.tenant_id,
                fail_on_required=False,
            )
            status = "ok" if not diagnostics else "degraded"
            return ReviewResult(request=req, status=status, diagnostics=list(diagnostics))
        except Exception as exc:
            return ReviewResult(
                request=req,
                status="error",
                diagnostics=[],
                error=str(exc),
            )

    def _store_result(self, result: ReviewResult) -> None:
        with self._result_lock:
            self._results.append(result)
            if len(self._results) > self._result_buffer:
                del self._results[: len(self._results) - self._result_buffer]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_worker: SkillReviewWorker | None = None
_worker_lock = threading.Lock()


def get_review_worker(
    catalog: Any | None = None,
    config: Any | None = None,
) -> SkillReviewWorker:
    """Return (or lazily create) the process-global review worker."""
    global _default_worker
    with _worker_lock:
        if _default_worker is None:
            _default_worker = SkillReviewWorker(catalog=catalog, config=config)
        else:
            if catalog is not None:
                _default_worker._catalog = catalog
            if config is not None:
                _default_worker._config = config
        return _default_worker
