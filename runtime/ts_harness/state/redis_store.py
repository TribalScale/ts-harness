"""Redis-backed shared state with in-memory fallback for local dev."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol


class StateStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...
    def incr(self, key: str, *, ttl_seconds: int | None = None) -> int: ...
    def delete(self, key: str) -> None: ...
    def get_json(self, key: str) -> Any | None: ...
    def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}

    def _expired(self, key: str) -> bool:
        if key not in self._data:
            return True
        _, expires = self._data[key]
        return expires is not None and time.time() > expires

    def get(self, key: str) -> str | None:
        if self._expired(key):
            self._data.pop(key, None)
            return None
        return self._data[key][0]

    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        expires = time.time() + ttl_seconds if ttl_seconds else None
        self._data[key] = (value, expires)

    def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        current = int(self.get(key) or "0") + 1
        self.set(key, str(current), ttl_seconds=ttl_seconds)
        return current

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def get_json(self, key: str) -> Any | None:
        raw = self.get(key)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        self.set(key, json.dumps(value), ttl_seconds=ttl_seconds)


class RedisStateStore:
    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        if ttl_seconds:
            self._client.setex(key, ttl_seconds, value)
        else:
            self._client.set(key, value)

    def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        count = int(self._client.incr(key))
        if ttl_seconds and count == 1:
            self._client.expire(key, ttl_seconds)
        return count

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def get_json(self, key: str) -> Any | None:
        raw = self.get(key)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        self.set(key, json.dumps(value), ttl_seconds=ttl_seconds)


_store: StateStore | None = None


def get_state_store() -> StateStore:
    global _store
    if _store is not None:
        return _store
    redis_url = os.environ.get("TS_HARNESS_REDIS_URL", "")
    if redis_url:
        try:
            _store = RedisStateStore(redis_url)
            return _store
        except Exception:
            pass
    _store = InMemoryStateStore()
    return _store
