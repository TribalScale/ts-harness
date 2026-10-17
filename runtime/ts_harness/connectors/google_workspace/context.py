"""Resolve impersonation email from agent connector_bindings."""

from __future__ import annotations

from ts_harness.config.schema import AgentConfig, GoogleWorkspaceConnectorConfig, RuntimeConfig


def resolve_impersonate_email(
    agent: AgentConfig,
    config: RuntimeConfig,
    gw_config: GoogleWorkspaceConnectorConfig,
) -> str | None:
    binding = (agent.connector_bindings or {}).get("google-workspace")
    if binding:
        acct = gw_config.accounts.get(binding)
        if acct and acct.impersonate_email.strip():
            return acct.impersonate_email.strip()
    delegation = gw_config.auth.delegation
    if delegation and delegation.default_impersonate_email.strip():
        return delegation.default_impersonate_email.strip()
    return None
