"""Secret resolution with redaction guarantees."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable
from typing import Any


SecretProvider = Callable[[str], str]


@dataclass
class RotationMetadata:
    version: str
    rotated_at: str


class SecretResolver:
    def __init__(self, namespace: str = "ts-harness", provider: SecretProvider | None = None):
        self.namespace = namespace
        self.provider = provider
        self._cache: dict[str, str] = {}
        self._rotations: dict[str, RotationMetadata] = {}

    def resolve(self, name: str, *, tenant_id: str = "default") -> str:
        key = f"{self.namespace}/{tenant_id}/{name}"
        if key in self._cache:
            return self._cache[key]
        if self.provider is not None:
            value = self.provider(key)
        else:
            env_name = name.upper().replace("-", "_")
            value = os.environ.get(env_name, "")
        self._cache[key] = value
        return value

    def redact(self, text: str) -> str:
        redacted = text
        for value in self._cache.values():
            if value and len(value) > 4:
                redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    def health(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "cached_keys": len(self._cache),
            "rotations": {
                key: {"version": value.version, "rotated_at": value.rotated_at}
                for key, value in self._rotations.items()
            },
        }

    def mark_rotated(self, name: str, *, tenant_id: str = "default", version: str) -> None:
        key = f"{self.namespace}/{tenant_id}/{name}"
        self._cache.pop(key, None)
        self._rotations[key] = RotationMetadata(
            version=version,
            rotated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def reload(self, name: str, *, tenant_id: str = "default") -> None:
        key = f"{self.namespace}/{tenant_id}/{name}"
        self._cache.pop(key, None)
        env_name = name.upper().replace("-", "_")
        self._cache.pop(env_name, None)

    def reload_all(self) -> None:
        self._cache.clear()


_RESOLVER: SecretResolver | None = None


def get_secret_resolver() -> SecretResolver:
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = SecretResolver()
    return _RESOLVER
