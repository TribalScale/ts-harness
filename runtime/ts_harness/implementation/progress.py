"""Where the coding run in this process is, for the worker's heartbeat (and so the Live page).

The backend marks each phase as it starts; the heartbeat thread reads a snapshot. A coding
worker runs one job per process, so module state is enough.
"""

from __future__ import annotations

import threading
import time
from typing import Any

PHASES = ("setup", "coding", "verification", "review", "push")

_lock = threading.Lock()
_state: dict[str, Any] = {"phase": "", "since": 0.0, "detail": "", "done": {}}


def reset() -> None:
    with _lock:
        _state.update(phase="", since=0.0, detail="", done={})


def begin(phase: str, done: dict[str, float] | None = None) -> None:
    """``phase`` starts now; ``done`` is the seconds of the phases finished so far."""
    with _lock:
        _state.update(phase=phase, since=time.monotonic(), detail="")
        if done is not None:
            _state["done"] = dict(done)


def detail(text: str) -> None:
    """What the current phase is doing ("fix pass 1", "review round 2")."""
    with _lock:
        _state["detail"] = str(text or "")[:200]


def snapshot() -> dict[str, Any] | None:
    with _lock:
        if not _state["phase"]:
            return None
        return {
            "phase": _state["phase"],
            "phase_seconds": round(time.monotonic() - _state["since"], 1),
            "detail": _state["detail"],
            "done": dict(_state["done"]),
        }
