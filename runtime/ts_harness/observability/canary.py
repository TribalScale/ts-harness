"""Canary traffic shaping – async shadow dispatch.

``shadow_dispatch`` fires a secondary call to a canary target in a
background thread so the primary response is never delayed.  Results are
compared via an optional ``on_result`` callback and any discrepancies can be
forwarded to the metrics registry or audit log.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


def shadow_dispatch(
    primary_result: Any,
    shadow_fn: Callable[[], Any],
    *,
    on_result: Callable[[Any, Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Fire *shadow_fn* asynchronously and optionally compare to *primary_result*.

    The call is fully non-blocking from the caller's perspective.  Any
    exception raised by *shadow_fn* is swallowed (after calling *on_error* if
    supplied) so as never to affect the primary code path.

    Args:
        primary_result: The already-produced result from the primary path.
        shadow_fn: Callable that executes the canary request.  Must be safe
            to call from a daemon thread.
        on_result: Optional two-arg callback ``(primary, shadow)`` invoked
            after both results are available.
        on_error: Optional one-arg callback invoked with any exception raised
            by *shadow_fn*.
    """

    def _run() -> None:
        try:
            shadow_result = shadow_fn()
            if on_result is not None:
                try:
                    on_result(primary_result, shadow_result)
                except Exception:
                    pass
        except Exception as exc:
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:
                    pass

    thread = threading.Thread(target=_run, daemon=True, name="canary-shadow")
    thread.start()


class CanaryRouter:
    """Routes a configurable percentage of traffic to a canary target.

    ``CanaryRouter`` wraps a primary dispatch callable and a canary callable.
    For each call it always invokes the primary and, based on *canary_pct*,
    may also invoke the canary via :func:`shadow_dispatch`.
    """

    def __init__(
        self,
        primary_fn: Callable[..., Any],
        canary_fn: Callable[..., Any],
        *,
        canary_pct: float = 0.0,
        on_result: Callable[[Any, Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if not 0.0 <= canary_pct <= 100.0:
            raise ValueError("canary_pct must be between 0 and 100")
        self._primary = primary_fn
        self._canary = canary_fn
        self._pct = canary_pct
        self._on_result = on_result
        self._on_error = on_error
        self._call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke primary; shadow-dispatch to canary if within percentage."""
        self._call_count += 1
        result = self._primary(*args, **kwargs)

        # Simple deterministic sampling: shadow every N-th request.
        if self._pct > 0.0:
            import random

            if random.uniform(0.0, 100.0) < self._pct:
                captured_args = args
                captured_kwargs = kwargs
                shadow_dispatch(
                    result,
                    lambda: self._canary(*captured_args, **captured_kwargs),
                    on_result=self._on_result,
                    on_error=self._on_error,
                )

        return result
