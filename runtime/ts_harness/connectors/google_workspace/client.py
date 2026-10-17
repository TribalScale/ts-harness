"""Build Google API clients for connector tool handlers."""

from __future__ import annotations

from ts_harness.config.schema import GoogleWorkspaceConnectorConfig, RuntimeConfig
from ts_harness.connectors.google_workspace.auth import delegation, oauth_store
from ts_harness.connectors.google_workspace.context import resolve_impersonate_email
from ts_harness.tools.policy import ToolPolicyContext

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/documents",
]


class GoogleClientFactory:
    def __init__(
        self,
        gw_config: GoogleWorkspaceConnectorConfig,
        runtime_config: RuntimeConfig,
    ):
        self.gw_config = gw_config
        self.runtime_config = runtime_config
        self.scopes = gw_config.scopes or DEFAULT_SCOPES

    def _oauth_credentials(self, ctx: ToolPolicyContext):
        actor = (ctx.actor_email or "").strip()
        if not actor:
            return None
        from ts_harness.connectors.google_workspace.auth import oauth_flow

        if oauth_flow.oauth_client_config() is None:
            return None
        client_id, client_secret, _ = oauth_flow.oauth_client_config()
        token = oauth_store.refresh_oauth_token(
            tenant_id=ctx.tenant_id,
            email=actor,
            client_id=client_id,
            client_secret=client_secret,
        )
        if not token:
            stored = oauth_store.get_oauth_credentials(tenant_id=ctx.tenant_id, email=actor)
            token = stored["access_token"] if stored else None
        if not token:
            return None
        from google.oauth2.credentials import Credentials

        return Credentials(token=token)

    def _delegation_credentials(self, ctx: ToolPolicyContext):
        agent = self.runtime_config.get_agent(ctx.agent_id)
        subject = resolve_impersonate_email(agent, self.runtime_config, self.gw_config)
        if not subject:
            raise RuntimeError(
                "No delegation mailbox configured: set connector_bindings on the agent "
                "or connectors.google-workspace.auth.delegation.default_impersonate_email"
            )
        return delegation.build_delegated_credentials(self.scopes, subject)

    def get_credentials(self, ctx: ToolPolicyContext):
        oauth_creds = self._oauth_credentials(ctx)
        if oauth_creds is not None:
            return oauth_creds, "oauth"
        return self._delegation_credentials(ctx), "delegation"

    def build(self, service: str, version: str, ctx: ToolPolicyContext):
        creds, _mode = self.get_credentials(ctx)
        from googleapiclient.discovery import build

        return build(service, version, credentials=creds, cache_discovery=False)
