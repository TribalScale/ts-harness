"""Declarative toolset composition adapted from Hermes toolsets.py (MIT)."""

from __future__ import annotations

from ts_harness.config.schema import AgentConfig, RuntimeConfig
from ts_harness.tools.mcp_registry import registered_mcp_tool_names
from ts_harness.connectors.tool_names import registered_google_workspace_tool_names


TOOLSET_DEFINITIONS = {
    "core-read": {"recall_memory", "read_file", "session_status", "read_skill_reference"},
    "core-write": {"write_file", "patch_file"},
    "terminal": {"exec"},
    "workflow": {"workflow_update"},
    "mcp": set(),
    "google-workspace": set(),
    "google-workspace-docs": set(),
    "channel": {"channel_send"},
    "delegation": {"delegate_task", "propose_delegate_task"},
    "channel-chat": {"propose_delegate_task"},
    "program-builder": {"builder_catalog", "propose_program_bundle"},
}


def _config_toolset_tools(name: str, config: RuntimeConfig) -> set[str]:
    return set(config.toolsets.get(name, []))


def expand_toolsets(agent: AgentConfig, config: RuntimeConfig) -> set[str]:
    names = set(agent.toolsets)
    for skill_category in agent.skills:
        if skill_category in config.toolsets:
            names.add(skill_category)
    tools: set[str] = set()
    for name in names:
        custom = set(config.toolsets.get(name, []))
        builtin = set(TOOLSET_DEFINITIONS.get(name, set()))
        if name == "mcp":
            tools.update(custom or registered_mcp_tool_names())
        elif name == "google-workspace":
            tools.update(custom or registered_google_workspace_tool_names())
        else:
            tools.update(custom or builtin)
    return tools


def agent_effective_tools(agent: AgentConfig, config: RuntimeConfig) -> set[str]:
    if agent.tool_allow:
        tools = set(agent.tool_allow)
        if "mcp" in agent.toolsets:
            tools.update(registered_mcp_tool_names())
        if "google-workspace" in agent.toolsets:
            tools.update(registered_google_workspace_tool_names())
        for name in agent.toolsets:
            if name in {"mcp", "google-workspace"}:
                continue
            custom = _config_toolset_tools(name, config)
            if custom:
                tools.update(custom)
        return tools
    return expand_toolsets(agent, config)
