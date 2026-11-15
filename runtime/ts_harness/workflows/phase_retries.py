"""Program phase completions that failed, kept for ``retry_program_phases`` to run again.

When a program workflow ends, the engine completes the program phase: it snapshots the
outputs and moves the program on. If that raises (memory API down, an embedding error), the
program would stay at its phase for ever. The engine records the workflow here; the system
job retries it with backoff and tells the program's chat when it recovers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

NAMESPACE = "programs"
KEY = "phase-completion-retries"
BASE_DELAY_SECONDS = 300
MAX_DELAY_SECONDS = 3600


def _load(memory: Any) -> tuple[dict[str, Any], int]:
    stored = memory.kv_get(NAMESPACE, KEY) or {}
    value = stored.get("value")
    return (dict(value) if isinstance(value, dict) else {}), int(stored.get("version") or 0)


def _update(memory: Any, change) -> Any:
    """Apply ``change(entries)`` with optimistic locking; returns what ``change`` returned."""
    from ts_harness.memory.client import MemoryApiConflict

    for _ in range(5):
        entries, version = _load(memory)
        result = change(entries)
        try:
            memory.kv_put(NAMESPACE, KEY, entries, expected_version=version)
            return result
        except MemoryApiConflict:
            continue
    raise RuntimeError("phase retry record kept changing; giving up")


def record_failure(memory: Any, *, workflow_id: str, workflow_type: str, tenant_id: str, error: str,
                   now: float | None = None) -> bool:
    """Record or update a failed completion. True when the workflow was not already queued."""
    now = time.time() if now is None else now

    def change(entries: dict[str, Any]) -> bool:
        entry = entries.get(workflow_id)
        if entry is None:
            entries[workflow_id] = {"workflow_type": workflow_type, "tenant_id": tenant_id, "error": error[:500],
                                    "first_failed_at": now, "last_attempt_at": now, "attempts": 1}
            return True
        entry.update(error=error[:500], last_attempt_at=now, attempts=int(entry.get("attempts") or 0) + 1)
        return False

    return bool(_update(memory, change))


def due(entry: dict[str, Any], now: float) -> bool:
    attempts = max(1, int(entry.get("attempts") or 1))
    delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempts - 1))
    return now - float(entry.get("last_attempt_at") or 0) >= delay


def _settled(memory: Any, workflow_id: str) -> bool:
    """True when retrying could only repeat work: the program moved on, ended, or the workflow is gone."""
    try:
        workflow = (memory.get_workflow(workflow_id) or {}).get("workflow") or {}
    except Exception:  # noqa: BLE001 - unknown state: keep it and try again later
        return False
    if not workflow or str(workflow.get("status") or "") in {"superseded", "cancelled"}:
        return True
    program_id = str(workflow.get("program_id") or "")
    if not program_id:
        return True
    try:
        program = memory.get_program(program_id) or {}
    except Exception:  # noqa: BLE001
        return False
    program = program.get("program") if isinstance(program.get("program"), dict) else program
    return str(program.get("status") or "running") != "running"


def retry_due(*, engine: Any = None, memory: Any = None, now: float | None = None) -> dict[str, Any]:
    """Run each due completion again; drop the ones that succeed."""
    from ts_harness.workflows.engine import get_workflow_engine

    engine = engine or get_workflow_engine()
    memory = memory or engine.client
    now = time.time() if now is None else now
    entries, _ = _load(memory)
    recovered: list[str] = []
    failed: list[str] = []
    for workflow_id, entry in entries.items():
        if not isinstance(entry, dict) or not due(entry, now):
            continue
        if _settled(memory, workflow_id):
            _update(memory, lambda items, wid=workflow_id: items.pop(wid, None))
            continue
        ctx = engine._context(workflow_id=workflow_id, workflow_type=str(entry.get("workflow_type") or ""),
                              tenant_id=str(entry.get("tenant_id") or "default"), actor="phase-retry")
        try:
            engine.registry.invoke("complete_program_phase", ctx)
        except Exception as exc:  # noqa: BLE001 - recorded and retried later
            logger.warning("phase completion retry failed workflow_id=%s: %s", workflow_id, exc)
            record_failure(memory, workflow_id=workflow_id, workflow_type=str(entry.get("workflow_type") or ""),
                           tenant_id=str(entry.get("tenant_id") or "default"), error=str(exc), now=now)
            failed.append(workflow_id)
            continue
        _update(memory, lambda items, wid=workflow_id: items.pop(wid, None))
        ctx.extras["channel_text"] = "The program moved to its next phase after an automatic retry."
        engine._maybe_notify(ctx, "blocked")
        recovered.append(workflow_id)
    return {"status": "ok", "recovered": recovered, "still_failing": failed}
