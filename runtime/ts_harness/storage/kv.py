"""Small JSON values shared by every replica, kept in ``runtime_kv`` through the memory API."""

from __future__ import annotations

import copy
import time
from typing import Any, Callable

CAS_RETRIES = 8


class KvConflict(RuntimeError):
    """The value kept changing under us; the update gave up after ``CAS_RETRIES`` attempts."""


def _client(client: Any) -> Any:
    if client is not None:
        return client
    from ts_harness.memory.client import get_memory_client

    return get_memory_client()


def kv_read(namespace: str, key: str, *, default: Any = None, client: Any = None) -> Any:
    value = _client(client).kv_get(namespace, key).get("value")
    return copy.deepcopy(default) if value is None else value


def kv_update(
    namespace: str,
    key: str,
    change: Callable[[Any], Any],
    *,
    default: Any = None,
    client: Any = None,
    retries: int = CAS_RETRIES,
) -> Any:
    """Read, apply ``change`` and write back only if nobody wrote in between; retry on a conflict."""
    from ts_harness.memory.client import MemoryApiConflict

    api = _client(client)
    for attempt in range(retries):
        current = api.kv_get(namespace, key)
        value = current.get("value")
        version = int(current.get("version") or 0)
        updated = change(copy.deepcopy(default) if value is None else value)
        try:
            api.kv_put(namespace, key, updated, expected_version=version)
            return updated
        except MemoryApiConflict as exc:
            if exc.code != "version_conflict":
                raise
            time.sleep(min(0.01 * (2**attempt), 0.2))
    raise KvConflict(f"{namespace}/{key} kept changing; gave up after {retries} attempts")
