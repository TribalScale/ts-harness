"""Tool registry adapted from Hermes tools/registry.py (MIT)."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable
import time

from ts_harness.config.schema import AgentConfig, RuntimeConfig
from ts_harness.policy.opa import get_opa_client
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy


ToolHandler = Callable[[dict[str, Any], ToolPolicyContext], Any]


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    toolsets: list[str]
    availability_check: Callable[[], bool] | None = None


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._generation = 0
        self._lock = Lock()
        self._defs_cache: dict[tuple, list[dict[str, Any]]] = {}
        self._availability_cache: dict[str, tuple[float, bool]] = {}
        self._availability_ttl = 30.0

    def register(self, tool: ToolDefinition, *, override: bool = False) -> None:
        with self._lock:
            if tool.name in self._tools and not override:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool
            self._generation += 1
            self._defs_cache.clear()

    def deregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)
            self._generation += 1
            self._defs_cache.clear()

    def snapshot(self) -> dict[str, ToolDefinition]:
        with self._lock:
            return dict(self._tools)

    def _is_available(self, tool: ToolDefinition) -> bool:
        if tool.availability_check is None:
            return True
        now = time.time()
        cached = self._availability_cache.get(tool.name)
        if cached and now - cached[0] < self._availability_ttl:
            return cached[1]
        try:
            ok = bool(tool.availability_check())
        except Exception:
            ok = False
        self._availability_cache[tool.name] = (now, ok)
        return ok

    def definitions_for_agent(
        self,
        agent: AgentConfig,
        ctx: ToolPolicyContext,
        config: RuntimeConfig,
    ) -> list[dict[str, Any]]:
        stage_tools = None if ctx.stage_tool_allow is None else frozenset(ctx.stage_tool_allow)
        cache_key = (
            agent.id,
            ctx.source,
            ctx.is_subagent,
            stage_tools,
            ctx.approval_resumable,
            self._generation,
        )
        if cache_key in self._defs_cache:
            return self._defs_cache[cache_key]
        advertised = {"allow", "approval_required"} if ctx.approval_resumable else {"allow"}
        defs: list[dict[str, Any]] = []
        for tool in self.snapshot().values():
            if not self._is_available(tool):
                continue
            if evaluate_tool_policy(
                tool.name,
                agent,
                config,
                ctx,
                opa_client=get_opa_client(),
            ).decision not in advertised:
                continue
            defs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
            )
        self._defs_cache[cache_key] = defs
        return defs

    def dispatch(self, name: str, args: dict[str, Any], *, context: ToolPolicyContext) -> Any:
        tool = self.snapshot().get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool.handler(args, context)


def build_default_registry() -> ToolRegistry:
    from ts_harness.tools.core_tools import register_core_tools
    from ts_harness.tools.program_builder_tools import register_program_builder_tools

    registry = ToolRegistry()
    register_core_tools(registry)
    register_program_builder_tools(registry)
    return registry
