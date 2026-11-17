"""Append-only audit event writer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any
import json
import logging
import time
import uuid


SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "authorization"}
RETRY_BUFFER_LIMIT = 10_000
_FLUSH_BATCH = 500

logger = logging.getLogger(__name__)


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
    """Audit events in memory plus a JSONL file, or in Postgres (``runtime_audit_events``) when ``shared``.

    In shared mode events the memory API could not take wait in a bounded
    buffer and go out with the next append; the oldest are dropped, with a
    warning, once the buffer is full.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        shared: bool | None = None,
        client: Any = None,
        buffer_limit: int = RETRY_BUFFER_LIMIT,
    ):
        if shared is None:
            from ts_harness.storage.backend import shared_state_enabled

            shared = shared_state_enabled()
        self.shared = shared
        self._client = client
        self.buffer_limit = buffer_limit
        self._pending: list[dict[str, Any]] = []
        self._sequence = 0
        self._events: list[AuditEvent] = []
        self._lock = Lock()
        self.path = Path(path) if path is not None and not shared else None
        if self.path and self.path.exists():
            self._load()

    @property
    def client(self) -> Any:
        if self._client is None:
            from ts_harness.memory.client import get_memory_client

            self._client = get_memory_client()
        return self._client

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
                sequence=(self._sequence if self.shared else len(self._events)) + 1,
                tenant_id=tenant_id,
                action_type=action_type,
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=self._redact(payload or {}),
            )
            if self.shared:
                self._sequence += 1
                self._pending.append(self._to_dict(event))
                self._flush_locked()
                return event
            self._events.append(event)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(self._to_dict(event), sort_keys=True) + "\n")
        return event

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def flush(self) -> bool:
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> bool:
        while self._pending:
            batch = self._pending[:_FLUSH_BATCH]
            try:
                self.client.append_audit_events(batch)
            except Exception as exc:  # noqa: BLE001
                overflow = len(self._pending) - self.buffer_limit
                if overflow > 0:
                    del self._pending[:overflow]
                    logger.warning("audit buffer full: dropped %d events the memory API could not take", overflow)
                logger.warning("audit events not stored yet (%d waiting): %s", len(self._pending), exc)
                return False
            del self._pending[: len(batch)]
        return True

    def export(
        self,
        *,
        tenant_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self.shared:
                self._flush_locked()
                stored = self.client.list_audit_events(tenant_id=tenant_id or "", since=since or "", until=until or "")
                waiting = self._filter([dict(e) for e in self._pending], tenant_id, since, until)
                return list(stored) + waiting
            return self._filter([self._to_dict(e) for e in self._events], tenant_id, since, until)

    @staticmethod
    def _filter(rows: list[dict[str, Any]], tenant_id: str | None, since: str | None, until: str | None) -> list[dict[str, Any]]:
        if tenant_id:
            rows = [e for e in rows if e["tenant_id"] == tenant_id]
        if since:
            rows = [e for e in rows if e["timestamp"] >= since]
        if until:
            rows = [e for e in rows if e["timestamp"] <= until]
        return rows

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
