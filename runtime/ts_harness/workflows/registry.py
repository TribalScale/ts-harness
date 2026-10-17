"""Hook registry and execution context for the workflow engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ts_harness.memory.client import MemoryClient


HookHandler = Callable[["HookContext"], dict[str, Any]]


@dataclass
class HookContext:
    workflow_id: str
    workflow_type: str
    tenant_id: str
    task: dict[str, Any] | None
    stage: Any
    skeleton: Any
    actor: str
    memory_client: MemoryClient
    reject_comments: str = ""
    session_metadata: dict[str, Any] = field(default_factory=dict)
    runner_event: str = ""
    dispatch_result: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    workflow: dict[str, Any] = field(default_factory=dict)


class HookRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, HookHandler] = {}

    def register(self, name: str, handler: HookHandler) -> None:
        self._handlers[name] = handler

    def get(self, name: str) -> HookHandler:
        try:
            return self._handlers[name]
        except KeyError as exc:
            raise KeyError(f"unknown hook: {name}") from exc

    def invoke(self, name: str, ctx: HookContext) -> dict[str, Any]:
        return self.get(name)(ctx)

    def names(self) -> list[str]:
        return sorted(self._handlers)
