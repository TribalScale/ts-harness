"""Simple in-process rate limiter for admin routes."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque


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


def build_admin_rate_limiter() -> RateLimiter:
    max_requests = int(os.environ.get("ADMIN_RATE_LIMIT_MAX", "120"))
    window = float(os.environ.get("ADMIN_RATE_LIMIT_WINDOW_SECONDS", "60"))
    return RateLimiter(max_requests=max_requests, window_seconds=window)


_admin_limiter = build_admin_rate_limiter()


def check_admin_rate_limit(client_key: str) -> None:
    from ts_harness.gateway.auth import AuthError

    if os.environ.get("ADMIN_RATE_LIMIT_DISABLED", "").lower() in {"1", "true", "yes"}:
        return
    if not _admin_limiter.allow(client_key):
        raise AuthError("rate limit exceeded", status=429)
