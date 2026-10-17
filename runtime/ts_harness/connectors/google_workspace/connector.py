"""Google Workspace connector — registers tools and health checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ts_harness.config.schema import GoogleWorkspaceConnectorConfig, RuntimeConfig
from ts_harness.connectors.base import ConnectorHealth
from ts_harness.connectors.google_workspace.auth import delegation, oauth_flow
from ts_harness.connectors.google_workspace.client import GoogleClientFactory, DEFAULT_SCOPES
from ts_harness.connectors.google_workspace.context import resolve_impersonate_email
from ts_harness.connectors.google_workspace.tools import (
    register_calendar_tools,
    register_docs_tools,
    register_drive_tools,
    register_gmail_tools,
)
from ts_harness.connectors.tool_names import set_google_workspace_tool_names
from ts_harness.tools.registry import ToolDefinition, ToolRegistry
from ts_harness.tools.policy import ToolPolicyContext

if TYPE_CHECKING:
    from ts_harness.secrets.resolver import SecretResolver

GOOGLE_LIBS_AVAILABLE = True
try:
    import googleapiclient  # noqa: F401
except ImportError:
    GOOGLE_LIBS_AVAILABLE = False


class GoogleWorkspaceConnector:
    connector_id = "google-workspace"

    def __init__(
        self,
        config: GoogleWorkspaceConnectorConfig,
        runtime_config: RuntimeConfig,
        secret_resolver: "SecretResolver | None" = None,
    ):
        self.config = config
        self.runtime_config = runtime_config
        self.secret_resolver = secret_resolver
        self._tool_names: set[str] = set()
        self._factory = GoogleClientFactory(config, runtime_config)

    def adopt_config(self, config: GoogleWorkspaceConnectorConfig) -> None:
        self.config = config
        self._factory.gw_config = config
        self._factory.scopes = list(config.scopes or DEFAULT_SCOPES)

    def registered_tool_names(self) -> set[str]:
        return set(self._tool_names)

    def register_tools(self, registry: ToolRegistry) -> None:
        if not GOOGLE_LIBS_AVAILABLE:
            return
        names: list[str] = []
        names.extend(register_gmail_tools(registry, self._factory))
        names.extend(register_calendar_tools(registry, self._factory))
        names.extend(register_drive_tools(registry, self._factory))
        names.extend(register_docs_tools(registry, self._factory))
        if oauth_flow.oauth_client_config():
            names.extend(self._register_oauth_admin_tools(registry))
        self._tool_names = set(names)
        set_google_workspace_tool_names(self._tool_names)

    def _register_oauth_admin_tools(self, registry: ToolRegistry) -> list[str]:
        def connect(_args: dict, ctx: ToolPolicyContext) -> dict:
            actor = (ctx.actor_email or "").strip()
            if not actor:
                return {"error": "actor_email required for OAuth connect"}
            url = oauth_flow.build_consent_url(tenant_id=ctx.tenant_id, email=actor)
            return {"action": "send_link", "url": url, "message": "Open this URL to connect Google."}

        def disconnect(_args: dict, ctx: ToolPolicyContext) -> dict:
            from ts_harness.connectors.google_workspace.auth import oauth_store

            actor = (ctx.actor_email or "").strip()
            if not actor:
                return {"error": "actor_email required"}
            oauth_store.delete_oauth_credentials(tenant_id=ctx.tenant_id, email=actor)
            return {"disconnected": True}

        registry.register(
            ToolDefinition(
                name="google_workspace_connect",
                description="Connect personal Google account via OAuth consent URL.",
                parameters={"type": "object", "properties": {}},
                handler=connect,
                toolsets=["google-workspace"],
            )
        )
        registry.register(
            ToolDefinition(
                name="google_workspace_disconnect",
                description="Disconnect stored Google OAuth credentials for the actor.",
                parameters={"type": "object", "properties": {}},
                handler=disconnect,
                toolsets=["google-workspace"],
            )
        )
        return ["google_workspace_connect", "google_workspace_disconnect"]

    def health(self) -> ConnectorHealth:
        modes: list[str] = []
        if self.config.auth.delegation:
            modes.append("delegation")
        if oauth_flow.oauth_client_config():
            modes.append("oauth")
        if not GOOGLE_LIBS_AVAILABLE:
            return ConnectorHealth(
                status="unavailable",
                detail="google-api-python-client not installed",
                auth_modes=modes,
            )
        subject = ""
        if self.config.auth.delegation:
            subject = self.config.auth.delegation.default_impersonate_email.strip()
            if not subject:
                for agent in self.runtime_config.agents:
                    subject = resolve_impersonate_email(agent, self.runtime_config, self.config) or ""
                    if subject:
                        break
        if not subject and "delegation" in modes:
            return ConnectorHealth(status="degraded", detail="no impersonate email configured", auth_modes=modes)
        if subject:
            ok, detail = delegation.probe_delegation(self.config.scopes or DEFAULT_SCOPES, subject)
            if not ok:
                return ConnectorHealth(status="degraded", detail=detail, auth_modes=modes)
        return ConnectorHealth(status="ok", auth_modes=modes)
