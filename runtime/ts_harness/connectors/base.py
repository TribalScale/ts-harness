"""Connector protocol and shared types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ts_harness.tools.registry import ToolRegistry


@dataclass
class ConnectorHealth:
    status: str  # ok | degraded | skipped | unavailable
    detail: str | None = None
    auth_modes: list[str] = field(default_factory=list)


@dataclass
class ConnectorContext:
    tenant_id: str
    agent_id: str
    actor_email: str | None = None


class ConnectorProtocol(Protocol):
    connector_id: str

    def register_tools(self, registry: ToolRegistry) -> None: ...

    def health(self) -> ConnectorHealth: ...

    def registered_tool_names(self) -> set[str]: ...
