"""Tool decision and invocation audit helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
import time
import uuid


@dataclass
class ToolAuditEvent:
    event_id: str
    tenant_id: str
    agent_id: str
    tool_name: str
    decision: str
    policy_id: str
    correlation_id: str | None = None
    args_redacted: dict[str, Any] = field(default_factory=dict)
    result_preview: str | None = None
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


class ToolAuditLog:
    def __init__(self, sink: Callable[[ToolAuditEvent], None] | None = None):
        self.events: list[ToolAuditEvent] = []
        self.sink = sink

    def record(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        tool_name: str,
        decision: str,
        policy_id: str,
        correlation_id: str | None = None,
        args: dict[str, Any] | None = None,
        result_preview: str | None = None,
    ) -> ToolAuditEvent:
        redacted = {}
        for key, value in (args or {}).items():
            if any(token in key.lower() for token in ("password", "token", "secret", "key")):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = value
        event = ToolAuditEvent(
            event_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            agent_id=agent_id,
            tool_name=tool_name,
            decision=decision,
            policy_id=policy_id,
            correlation_id=correlation_id,
            args_redacted=redacted,
            result_preview=(result_preview or "")[:500] or None,
        )
        self.events.append(event)
        if self.sink:
            self.sink(event)
        return event


_default_log = ToolAuditLog()


def get_tool_audit_log() -> ToolAuditLog:
    return _default_log


def configure_tool_audit(sink: Callable[[ToolAuditEvent], None] | None) -> None:
    global _default_log
    _default_log = ToolAuditLog(sink=sink)
