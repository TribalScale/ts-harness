"""Wall-clock request timeout selection for the agent-runtime gateway."""

from __future__ import annotations

import os

_REQUEST_TIMEOUT = float(os.environ.get("TS_HARNESS_REQUEST_TIMEOUT", "300"))
_DISPATCH_REQUEST_TIMEOUT = float(
    os.environ.get("TS_HARNESS_DISPATCH_REQUEST_TIMEOUT", "7200")
)


def resolve_request_timeout(
    method: str,
    path: str,
    *,
    default_timeout: float | None = None,
    dispatch_timeout: float | None = None,
) -> float:
    """Return wall-clock timeout for a request; dispatch runs can take hours."""
    default = float(_REQUEST_TIMEOUT if default_timeout is None else default_timeout)
    dispatch = float(
        _DISPATCH_REQUEST_TIMEOUT if dispatch_timeout is None else dispatch_timeout
    )
    if method.upper() == "POST" and path.rstrip("/") == "/workflow/dispatch":
        return max(60.0, dispatch)
    return max(1.0, default)
