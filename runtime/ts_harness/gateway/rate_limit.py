"""Admin route rate limit: shared through Postgres across replicas, in-process otherwise."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable

logger = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window limiter keyed by client identity."""

    def __init__(self, *, max_requests: int = 60, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                return False
            bucket.append(now)
            return True


class SharedRateLimiter:
    """Fixed windows in ``rate_limit_windows`` (migration 039), counted across every replica."""

    def __init__(self, connect: Callable[[], Any], *, max_requests: int, window_seconds: float, fallback: RateLimiter):
        self.connect = connect
        self.max_requests = max_requests
        self.window_seconds = max(1, int(window_seconds))
        self.fallback = fallback

    def allow(self, key: str) -> bool:
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rate_limit_windows (key, window_start, hits)
                    VALUES (%(key)s, to_timestamp(floor(extract(epoch FROM now()) / %(w)s) * %(w)s), 1)
                    ON CONFLICT (key, window_start) DO UPDATE SET hits = rate_limit_windows.hits + 1
                    RETURNING hits
                    """,
                    {"key": key, "w": self.window_seconds},
                )
                hits = int(cur.fetchone()[0])
                if random.random() < 0.01:
                    cur.execute("DELETE FROM rate_limit_windows WHERE window_start < now() - interval '1 hour'")
            return hits <= self.max_requests
        except Exception as exc:  # noqa: BLE001
            logger.warning("shared rate limit unavailable, using this process's: %s", exc)
            return self.fallback.allow(key)


def build_admin_rate_limiter() -> RateLimiter:
    max_requests = int(os.environ.get("ADMIN_RATE_LIMIT_MAX", "120"))
    window = float(os.environ.get("ADMIN_RATE_LIMIT_WINDOW_SECONDS", "60"))
    return RateLimiter(max_requests=max_requests, window_seconds=window)


_admin_limiter = build_admin_rate_limiter()
_shared_limiter: SharedRateLimiter | None = None


def _limiter() -> RateLimiter | SharedRateLimiter:
    global _shared_limiter
    from ts_harness.agent.sessions import shared_state_connection

    connect = shared_state_connection()
    if connect is None:
        return _admin_limiter
    if _shared_limiter is None:
        _shared_limiter = SharedRateLimiter(
            connect,
            max_requests=_admin_limiter.max_requests,
            window_seconds=_admin_limiter.window_seconds,
            fallback=_admin_limiter,
        )
    return _shared_limiter


def check_admin_rate_limit(client_key: str) -> None:
    from ts_harness.gateway.auth import AuthError

    if os.environ.get("ADMIN_RATE_LIMIT_DISABLED", "").lower() in {"1", "true", "yes"}:
        return
    if not _limiter().allow(client_key):
        raise AuthError("rate limit exceeded", status=429)
