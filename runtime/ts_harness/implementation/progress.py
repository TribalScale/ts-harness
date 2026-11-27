"""Where the coding run in this process is, for the worker's heartbeat (and so the Live page).

The backend marks each phase as it starts, and each step inside it ("reviewing (round 1)",
"fixing 2 review findings", "pushing the branch") as that starts. Every step is timed, so the
card can show that a 27-minute "Pre-push review" was 2 minutes of reviewing and 25 of fixing
and re-testing. The heartbeat thread reads a snapshot. A coding worker runs one job per
process, so module state is enough.
"""

from __future__ import annotations

import threading
from time import monotonic as _clock
from typing import Any

PHASES = ("setup", "coding", "verification", "review", "push")
MAX_STEPS_PER_PHASE = 12
LABEL_CHARS = 80

_lock = threading.Lock()
_state: dict[str, Any] = {"phase": "", "since": 0.0, "detail": "", "done": {}, "steps": {}, "step_since": 0.0}


def reset() -> None:
    with _lock:
        _state.update(phase="", since=0.0, detail="", done={}, steps={}, step_since=0.0)
        _events.clear()


def _close_step(now: float) -> None:
    """Record the running step's time under its phase (caller holds the lock)."""
    phase, label = _state["phase"], _state["detail"]
    if not phase or not label:
        return
    steps = _state["steps"].setdefault(phase, [])
    seconds = round(now - _state["step_since"], 1)
    if steps and steps[-1][0] == label:
        steps[-1][1] = round(steps[-1][1] + seconds, 1)
    elif len(steps) < MAX_STEPS_PER_PHASE:
        steps.append([label, seconds])


def begin(phase: str, done: dict[str, float] | None = None) -> None:
    """``phase`` starts now; ``done`` is the seconds of the phases finished so far."""
    with _lock:
        now = _clock()
        _close_step(now)
        _state.update(phase=phase, since=now, detail="", step_since=now)
        if done is not None:
            _state["done"] = dict(done)


def detail(text: str) -> None:
    """The step the current phase is on now ("fixing test failures (pass 1)"); the previous step ends."""
    label = str(text or "")[:LABEL_CHARS]
    with _lock:
        if label == _state["detail"]:
            return
        now = _clock()
        _close_step(now)
        _state.update(detail=label, step_since=now)


def _steps_view(now: float) -> dict[str, list[dict[str, Any]]]:
    view = {phase: [{"label": label, "seconds": seconds} for label, seconds in steps] for phase, steps in _state["steps"].items()}
    if _state["phase"] and _state["detail"]:
        view.setdefault(_state["phase"], []).append(
            {"label": _state["detail"], "seconds": round(now - _state["step_since"], 1), "active": True}
        )
    return view


def snapshot() -> dict[str, Any] | None:
    with _lock:
        if not _state["phase"]:
            return None
        now = _clock()
        return {
            "phase": _state["phase"],
            "phase_seconds": round(now - _state["since"], 1),
            "detail": _state["detail"],
            "done": dict(_state["done"]),
            "steps": _steps_view(now),
        }


def step_timings() -> dict[str, list[dict[str, Any]]]:
    """Finished steps per phase, for the job result (the running step counts up to now)."""
    with _lock:
        view = _steps_view(_clock())
        for steps in view.values():
            for step in steps:
                step.pop("active", None)
        return view


# -- file events (see file_feed.py) -----------------------------------------------------------

MAX_BUFFERED_EVENTS = 2000
_events: list[dict[str, Any]] = []


def emit_events(events: list[dict[str, Any]]) -> None:
    """Queue file events for the worker to send; the oldest are dropped past ``MAX_BUFFERED_EVENTS``."""
    with _lock:
        _events.extend(events)
        if len(_events) > MAX_BUFFERED_EVENTS:
            del _events[: len(_events) - MAX_BUFFERED_EVENTS]


def take_events(limit: int = 200) -> list[dict[str, Any]]:
    """Up to ``limit`` queued events, oldest first; they leave the queue."""
    with _lock:
        taken = _events[:limit]
        del _events[:limit]
        return taken


def return_events(events: list[dict[str, Any]]) -> None:
    """Put events back at the front after a failed send."""
    with _lock:
        _events[:0] = events
        if len(_events) > MAX_BUFFERED_EVENTS:
            del _events[MAX_BUFFERED_EVENTS:]
