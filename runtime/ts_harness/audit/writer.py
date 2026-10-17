"""Append-only audit event writer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any
import json
import time
import uuid


SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "authorization"}


@dataclass
class AuditEvent:
    event_id: str
    sequence: int
    tenant_id: str
    action_type: str
    actor_id: str
    correlation_id: str | None
    payload: dict[str, Any]
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


class AuditWriter:
    def __init__(self, path: str | Path | None = None):
        self._events: list[AuditEvent] = []
        self._lock = Lock()
        self.path = Path(path) if path is not None else None
        if self.path and self.path.exists():
            self._load()

    def append(
        self,
        *,
        tenant_id: str,
        action_type: str,
        actor_id: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AuditEvent:
        with self._lock:
            event = AuditEvent(
                event_id=str(uuid.uuid4()),
                sequence=len(self._events) + 1,
                tenant_id=tenant_id,
                action_type=action_type,
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=self._redact(payload or {}),
            )
            self._events.append(event)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(self._to_dict(event), sort_keys=True) + "\n")
        return event

    def export(
        self,
        *,
        tenant_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._events
            if tenant_id:
                rows = [e for e in rows if e.tenant_id == tenant_id]
            if since:
                rows = [e for e in rows if e.timestamp >= since]
            if until:
                rows = [e for e in rows if e.timestamp <= until]
            return [self._to_dict(e) for e in rows]

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            self._events.append(
                AuditEvent(
                    event_id=row["event_id"],
                    sequence=int(row["sequence"]),
                    tenant_id=row["tenant_id"],
                    action_type=row["action_type"],
                    actor_id=row["actor_id"],
                    correlation_id=row.get("correlation_id"),
                    payload=row.get("payload") or {},
                    timestamp=row.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            )

    def _to_dict(self, event: AuditEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "tenant_id": event.tenant_id,
            "action_type": event.action_type,
            "actor_id": event.actor_id,
            "correlation_id": event.correlation_id,
            "payload": event.payload,
            "timestamp": event.timestamp,
        }

    def _redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if any(token in key.lower() for token in SENSITIVE_KEYS):
                redacted[key] = "[REDACTED]"
            elif isinstance(value, dict):
                redacted[key] = self._redact(value)
            else:
                redacted[key] = value
        return redacted
