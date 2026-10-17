"""Discovered MCP tool names shared across policy and runtime registration."""

from __future__ import annotations

_discovered_tool_names: set[str] = set()


def registered_mcp_tool_names() -> set[str]:
    return set(_discovered_tool_names)


def set_registered_mcp_tool_names(names: set[str]) -> None:
    global _discovered_tool_names
    _discovered_tool_names = set(names)
