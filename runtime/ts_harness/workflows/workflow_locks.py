"""Per-workflow write lock shared by every runner and runtime process.

``workflow_lock(client, workflow_id)`` holds the memory API lock for the block.
It is reentrant per thread, so engine entry points can nest. ``suspended``
releases it around long work such as an agent turn. A client without lock
support (tests, an older memory API) runs unlocked.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300
DEFAULT_WAIT_SECONDS = 30.0
UNAVAILABLE_BACKOFF_SECONDS = 300.0
_local = threading.local()
_unavailable_until: dict[int, float] = {}


class WorkflowLockTimeout(RuntimeError):
    """Another process held the workflow's lock for longer than the wait."""


def _held() -> dict[str, int]:
    held = getattr(_local, "held", None)
    if held is None:
        held = {}
        _local.held = held
    return held


def lock_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def _enabled(client: Any) -> bool:
    if os.environ.get("WORKFLOW_LOCKS_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        return False
    if _unavailable_until.get(id(client), 0.0) > time.monotonic():
        return False
    return callable(getattr(client, "acquire_workflow_lock", None))


def _ttl() -> int:
    return int(os.environ.get("WORKFLOW_LOCK_TTL_SECONDS") or DEFAULT_TTL_SECONDS)


def _acquire(client: Any, workflow_id: str, wait_seconds: float) -> bool:
    """True when held; False when the client cannot lock (the caller runs unlocked)."""
    deadline = time.monotonic() + wait_seconds
    delay = 0.05
    while True:
        try:
            if client.acquire_workflow_lock(workflow_id, owner=lock_owner(), ttl_seconds=_ttl()):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow lock unavailable; running unlocked for a while: %s", exc)
            _unavailable_until[id(client)] = time.monotonic() + UNAVAILABLE_BACKOFF_SECONDS
            return False
        if time.monotonic() >= deadline:
            raise WorkflowLockTimeout(f"workflow {workflow_id} is locked by another process")
        time.sleep(delay)
        delay = min(delay * 2, 1.0)


def _release(client: Any, workflow_id: str) -> None:
    try:
        client.release_workflow_lock(workflow_id, owner=lock_owner())
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow lock release failed workflow_id=%s: %s", workflow_id, exc)


@contextmanager
def workflow_lock(client: Any, workflow_id: Any, *, wait_seconds: float | None = None) -> Iterator[None]:
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id or not _enabled(client):
        yield
        return
    held = _held()
    if held.get(workflow_id):
        held[workflow_id] += 1
        try:
            yield
        finally:
            held[workflow_id] -= 1
        return
    wait = DEFAULT_WAIT_SECONDS if wait_seconds is None else wait_seconds
    if not _acquire(client, workflow_id, wait):
        yield
        return
    held[workflow_id] = 1
    try:
        yield
    finally:
        held.pop(workflow_id, None)
        _release(client, workflow_id)


@contextmanager
def suspended(client: Any, workflow_id: Any) -> Iterator[None]:
    """Release this thread's hold on the workflow for the block, then take it back."""
    workflow_id = str(workflow_id or "").strip()
    held = _held()
    depth = held.pop(workflow_id, 0) if workflow_id else 0
    if depth:
        _release(client, workflow_id)
    try:
        yield
    finally:
        if depth:
            _acquire(client, workflow_id, DEFAULT_WAIT_SECONDS)
            held[workflow_id] = depth
