"""Global registry of connector-registered tool names."""

from __future__ import annotations

_google_workspace_tool_names: set[str] = set()


def set_google_workspace_tool_names(names: set[str]) -> None:
    global _google_workspace_tool_names
    _google_workspace_tool_names = set(names)


def registered_google_workspace_tool_names() -> set[str]:
    return set(_google_workspace_tool_names)
