"""Tests for declarative Google Workspace toolset splits."""

from __future__ import annotations

import textwrap

import pytest

from ts_harness.config.schema import load_runtime_config
from ts_harness.tools.policy import ToolPolicyContext, evaluate_tool_policy
from ts_harness.tools.toolsets import agent_effective_tools


@pytest.fixture
def pm_docs_config_yaml(tmp_path):
    yaml_text = textwrap.dedent(
        """
        gateway:
          token: test-token
        tenants:
          - id: default
            display_name: Default
            enabled_connectors: [google-workspace]

        connectors:
          google-workspace:
            enabled: true
            accounts:
              pm:
                impersonate_email: pm@example.com

        toolsets:
          google-workspace-docs:
            - google_workspace_drive_search
            - google_workspace_drive_get
            - google_workspace_drive_create
            - google_workspace_docs_read
            - google_workspace_docs_append

        agents:
          list:
            - id: pm-agent
              default: true
              workspace: /workspace/pm-agent
              model: openai/gpt-4o
              connector_bindings:
                google-workspace: pm
              toolsets: [google-workspace-docs]
              tools:
                allow: [read_file, recall_memory]
                deny: [exec]
        """
    )
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml_text)
    return path


def test_google_workspace_docs_toolset_expands_for_agent_with_allow_list(pm_docs_config_yaml):
    config = load_runtime_config(pm_docs_config_yaml)
    agent = config.get_agent("pm-agent")
    tools = agent_effective_tools(agent, config)
    assert "google_workspace_docs_append" in tools
    assert "google_workspace_drive_create" in tools
    assert "read_file" in tools
    assert "google_workspace_gmail_search" not in tools


def test_pm_agent_docs_tools_allowed_when_connector_enabled(pm_docs_config_yaml):
    config = load_runtime_config(pm_docs_config_yaml)
    agent = config.get_agent("pm-agent")
    ctx = ToolPolicyContext(tenant_id="default", agent_id="pm-agent", source="workflow")
    decision = evaluate_tool_policy("google_workspace_docs_read", agent, config, ctx)
    assert decision.decision == "allow"


def test_pm_agent_gmail_denied_without_explicit_allow(pm_docs_config_yaml):
    config = load_runtime_config(pm_docs_config_yaml)
    agent = config.get_agent("pm-agent")
    ctx = ToolPolicyContext(tenant_id="default", agent_id="pm-agent", source="workflow")
    decision = evaluate_tool_policy("google_workspace_gmail_search", agent, config, ctx)
    assert decision.decision == "deny"
    assert decision.policy_id == "agent_allow"
