"""Tests for connector registry and config parsing."""

from __future__ import annotations

import textwrap

import pytest

from ts_harness.config.schema import load_runtime_config
from ts_harness.connectors.google_workspace.context import resolve_impersonate_email
from ts_harness.connectors.registry import ConnectorRegistry
from ts_harness.tools.registry import build_default_registry


@pytest.fixture
def google_config_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("TS_HARNESS_SKIP_GOOGLE_PROBE", "true")
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
            auth:
              delegation:
                default_impersonate_email: default@example.com
            accounts:
              comms:
                impersonate_email: comms@example.com
        agents:
          list:
            - id: comms-agent
              default: true
              workspace: /w
              model: openai/gpt-4o-mini
              connector_bindings:
                google-workspace: comms
              toolsets: [google-workspace]
        """
    )
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml_text)
    return path


def test_parse_connectors_block(google_config_yaml):
    config = load_runtime_config(google_config_yaml)
    assert config.connectors.google_workspace is not None
    assert config.connectors.google_workspace.accounts["comms"].impersonate_email == "comms@example.com"


def test_resolve_impersonate_email_from_binding(google_config_yaml):
    config = load_runtime_config(google_config_yaml)
    agent = config.get_agent("comms-agent")
    gw = config.connectors.google_workspace
    assert resolve_impersonate_email(agent, config, gw) == "comms@example.com"


def test_connector_registry_registers_tools(google_config_yaml, monkeypatch):
    monkeypatch.setenv("TS_HARNESS_SKIP_GOOGLE_PROBE", "true")
    config = load_runtime_config(google_config_yaml)
    registry = build_default_registry()
    cr = ConnectorRegistry(config)
    cr.register_enabled(registry)
    names = {t.name for t in registry.snapshot().values()}
    assert "google_workspace_gmail_search" in names
