"""Connector registry — loads enabled connectors from runtime config."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ts_harness.config.schema import RuntimeConfig
from ts_harness.connectors.base import ConnectorHealth, ConnectorProtocol
from ts_harness.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from ts_harness.secrets.resolver import SecretResolver

_registry_singleton: "ConnectorRegistry | None" = None


class ConnectorRegistry:
    def __init__(self, config: RuntimeConfig, secret_resolver: "SecretResolver | None" = None):
        self.config = config
        self.secret_resolver = secret_resolver
        self._connectors: dict[str, ConnectorProtocol] = {}

    def _tenant_has_connector(self, connector_id: str, tenant_id: str) -> bool:
        tenant = next((t for t in self.config.tenants if t.id == tenant_id), None)
        if tenant is None:
            return False
        return connector_id in set(tenant.enabled_connectors or [])

    def _load_google_workspace(self) -> ConnectorProtocol | None:
        cfg = self.config.connectors_dict.get("google-workspace")
        if not cfg or not cfg.enabled:
            return None
        from ts_harness.connectors.google_workspace.connector import GoogleWorkspaceConnector

        return GoogleWorkspaceConnector(cfg, self.config, self.secret_resolver)

    def load(self, tenant_id: str = "default") -> None:
        self._connectors.clear()
        if not self._tenant_has_connector("google-workspace", tenant_id):
            return
        gw = self._load_google_workspace()
        if gw is not None:
            self._connectors["google-workspace"] = gw

    def register_enabled(self, registry: ToolRegistry, *, tenant_id: str = "default") -> None:
        self.load(tenant_id=tenant_id)
        for connector in self._connectors.values():
            connector.register_tools(registry)

    def health(self, *, tenant_id: str = "default") -> ConnectorHealth:
        if not self._tenant_has_connector("google-workspace", tenant_id):
            return ConnectorHealth(status="skipped", detail="connector not enabled for tenant")
        if "google-workspace" not in self._connectors:
            self.load(tenant_id=tenant_id)
        connector = self._connectors.get("google-workspace")
        if connector is None:
            return ConnectorHealth(status="unavailable", detail="google-workspace not configured")
        return connector.health()

    def get_connector(self, connector_id: str) -> ConnectorProtocol | None:
        return self._connectors.get(connector_id)

    def list_tools(self, connector_id: str) -> list[dict[str, Any]]:
        connector = self._connectors.get(connector_id)
        if connector is None:
            return []
        return [
            {"name": name, "connector": connector_id}
            for name in sorted(connector.registered_tool_names())
        ]


def get_connector_registry() -> ConnectorRegistry | None:
    return _registry_singleton


def set_connector_registry(registry: ConnectorRegistry | None) -> None:
    global _registry_singleton
    _registry_singleton = registry
