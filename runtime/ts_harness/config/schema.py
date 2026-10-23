"""Runtime configuration schema for ts-harness-runtime.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import copy
import json
import re

import yaml


GLOBAL_DENY_TOOLS = {"browser", "gateway"}


@dataclass
class TeamGroupsConfig:
    prefix: str = "ts-harness-team-"
    approver_suffix: str = "-approvers"
    manager_suffix: str = "-managers"


@dataclass
class IdentityConfig:
    provider: str = "static"
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    client_id: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    scim_enabled: bool = False
    slack_user_emails: dict[str, str] = field(default_factory=dict)
    jira_account_ids: dict[str, str] = field(default_factory=dict)
    github_logins: dict[str, str] = field(default_factory=dict)
    roles: dict[str, list[str]] = field(default_factory=dict)
    role_bundles: dict[str, list[str]] = field(default_factory=dict)
    departments: dict[str, str] = field(default_factory=dict)
    service_permissions: list[str] | None = None
    group_roles: dict[str, list[str]] = field(default_factory=dict)
    tenant_claim: str = "tenant_id"
    preset: str | None = None
    token_type: str | None = None
    groups_claim: str | None = None
    roles_claim: str | None = None
    groups_source: str | None = None
    allowed_domains: list[str] = field(default_factory=list)
    directory: str | None = None
    team_groups: TeamGroupsConfig = field(default_factory=TeamGroupsConfig)


@dataclass
class AgentHeartbeatConfig:
    every: str = "30m"
    model: str | None = None
    target: str = "none"
    to: str | None = None
    light_context: bool = True
    isolated_session: bool = True
    active_hours_start: str = "07:00"
    active_hours_end: str = "22:00"
    timezone: str = "UTC"
    prompt: str = "Read HEARTBEAT.md. Reply HEARTBEAT_OK if nothing needs attention."


@dataclass
class GoogleWorkspaceAccountConfig:
    impersonate_email: str = ""


@dataclass
class GoogleWorkspaceDelegationAuth:
    credentials_secret: str = "google-workspace-sa-json"
    default_impersonate_email: str = ""


@dataclass
class GoogleWorkspaceOAuthAuth:
    client_id_secret: str = "google-oauth-client-id"
    client_secret_secret: str = "google-oauth-client-secret"
    callback_url: str = ""
    kms_key_id: str = ""


@dataclass
class GoogleWorkspaceConnectorAuth:
    delegation: GoogleWorkspaceDelegationAuth | None = None
    oauth: GoogleWorkspaceOAuthAuth | None = None


@dataclass
class GoogleWorkspaceArtifactLibraryConfig:
    enabled: bool = False
    publish_on: str = "present"
    share_folder_with_requester: bool = True
    program_subfolder: str = ""
    step_phase_map: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class GoogleWorkspaceArtifactPublishConfig:
    enabled: bool = True
    folder_id: str = ""
    min_chars: int = 3000
    chunk_chars: int = 50000
    library: GoogleWorkspaceArtifactLibraryConfig = field(
        default_factory=GoogleWorkspaceArtifactLibraryConfig
    )


@dataclass
class GoogleWorkspaceConnectorConfig:
    enabled: bool = True
    auth: GoogleWorkspaceConnectorAuth = field(default_factory=GoogleWorkspaceConnectorAuth)
    accounts: dict[str, GoogleWorkspaceAccountConfig] = field(default_factory=dict)
    scopes: list[str] = field(default_factory=list)
    artifact_publish: GoogleWorkspaceArtifactPublishConfig = field(
        default_factory=GoogleWorkspaceArtifactPublishConfig
    )


@dataclass
class ConnectorsConfig:
    google_workspace: GoogleWorkspaceConnectorConfig | None = None


@dataclass
class AgentConfig:
    id: str
    workspace: str
    model: str
    skills: list[str] = field(default_factory=list)
    tool_allow: list[str] = field(default_factory=list)
    tool_deny: list[str] = field(default_factory=list)
    toolsets: list[str] = field(default_factory=list)
    connector_bindings: dict[str, str] = field(default_factory=dict)
    # None inherits the catalog soul. "" is an explicit clear.
    soul: str | None = None
    default: bool = False
    subagent_allow: list[str] = field(default_factory=list)
    heartbeat: AgentHeartbeatConfig | None = None
    sandbox_mode: str = "off"


@dataclass
class TenantConfig:
    id: str
    display_name: str
    enabled_connectors: list[str] = field(default_factory=list)
    data_residency: str = "us"


@dataclass
class GatewayConfig:
    token: str = ""
    allowed_origins: list[str] = field(default_factory=list)
    single_tenant_mode: bool = True
    default_tenant_id: str = "default"


@dataclass
class SandboxConfig:
    mode: str = "docker"
    backend: str = "docker"
    cpu_limit: float = 1.0
    memory_mb: int = 512
    timeout_seconds: int = 300
    egress_policy: str = "deny"


GATE_AGENT_IDS = ("review-agent", "qa-agent")
FORBIDDEN_GATE_MODELS = frozenset({"openai/gpt-4o-mini", "gpt-4o-mini"})


@dataclass
class PolicyConfig:
    approval_required_tools: list[str] = field(default_factory=list)
    channel_ingress_deny: list[str] = field(default_factory=lambda: ["exec", "write", "delegation"])
    delegation_auto_allow_agents: list[str] = field(default_factory=lambda: ["pm-agent", "analyst-agent"])
    budget_warn_usd: float = 50.0
    budget_hard_stop_usd: float = 100.0
    data_residency_regions: dict[str, list[str]] = field(default_factory=dict)
    egress_sensitive_tools: list[str] = field(default_factory=lambda: ["exec", "mcp", "google_workspace"])
    network_egress_deny_tools: list[str] = field(default_factory=list)
    model_allowlist: dict[str, list[str]] = field(default_factory=dict)
    rate_limit_per_minute: int = 0
    opa_enabled: bool = False


@dataclass
class SubagentLimitsConfig:
    max_depth: int = 2
    max_children_per_agent: int = 5
    max_concurrent: int = 8


@dataclass
class RoutingIntentPolicy:
    mode: str = "require_workflow"
    deny_tools: list[str] = field(default_factory=list)
    deny_start_workflow_types: list[str] = field(default_factory=list)
    allow_program_types: list[str] = field(default_factory=list)


@dataclass
class RoutingIntentRule:
    id: str
    signals: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    workflow_type: str | None = None
    program_type: str | None = None
    entry_phase: str | None = None
    delegate_agent: str | None = None
    orchestrator_hint: str | None = None
    policy: RoutingIntentPolicy | None = None


@dataclass
class RoutingConfig:
    intents: list[RoutingIntentRule] = field(default_factory=list)
    default: str = "ad_hoc_delegate"


@dataclass
class OpenCodeBackendConfig:
    version: str = "latest"
    binary: str = "opencode"
    provider: str = ""
    auth_env: str = "CODING_BACKEND_API_KEY"
    model: str = ""
    run_args: list[str] = field(default_factory=lambda: ["run", "--auto"])
    verification_fix_passes: int = 2


@dataclass
class ImplementationRepoConfig:
    url: str = ""
    base_branch: str = "main"
    clone_depth: int = 1


@dataclass
class ImplementationBackendsConfig:
    opencode: OpenCodeBackendConfig = field(default_factory=OpenCodeBackendConfig)


@dataclass
class ImplementationConfig:
    coding_backend: str = "opencode"
    tools_root: str = ""
    backends: ImplementationBackendsConfig = field(default_factory=ImplementationBackendsConfig)
    repos: dict[str, ImplementationRepoConfig] = field(default_factory=dict)


@dataclass
class ProgramBuilderConfig:
    agent_id: str = "builder-agent"


@dataclass
class DeploymentConfig:
    profile: str = "single_host"
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class SchedulingConfig:
    """Coding job quotas: running jobs per requester, program and team (0 means no limit)."""

    coding_per_user: int = 2
    coding_per_program: int = 3
    coding_per_team: int = 8
    # None keeps the launcher's CODING_MAX_RUNNING.
    coding_global: int | None = None

    def quotas(self) -> dict[str, int]:
        return {
            "per_user": self.coding_per_user,
            "per_program": self.coding_per_program,
            "per_team": self.coding_per_team,
        }


@dataclass
class RuntimeConfig:
    tenants: list[TenantConfig]
    agents: list[AgentConfig]
    gateway: GatewayConfig
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    subagents: SubagentLimitsConfig = field(default_factory=SubagentLimitsConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    bindings: dict[str, str] = field(default_factory=dict)
    toolsets: dict[str, list[str]] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    connectors: ConnectorsConfig = field(default_factory=ConnectorsConfig)
    routing: RoutingConfig | None = None
    implementation: ImplementationConfig = field(default_factory=ImplementationConfig)
    program_defaults: dict[str, Any] = field(default_factory=dict)
    program_builder: ProgramBuilderConfig = field(default_factory=ProgramBuilderConfig)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)

    @property
    def connectors_dict(self) -> dict[str, GoogleWorkspaceConnectorConfig]:
        """Map connector id (kebab-case) to config for registry lookup."""
        out: dict[str, GoogleWorkspaceConnectorConfig] = {}
        if self.connectors.google_workspace is not None:
            out["google-workspace"] = self.connectors.google_workspace
        return out
    _last_known_good: dict[str, Any] | None = field(default=None, repr=False)

    def get_agent(self, agent_id: str) -> AgentConfig:
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        raise KeyError(f"unknown agent: {agent_id}")

    def default_agent(self) -> AgentConfig:
        for agent in self.agents:
            if agent.default:
                return agent
        raise ValueError("no default orchestrator agent configured")

    def validate(self) -> None:
        if not self.agents:
            raise ValueError("at least one agent is required")
        defaults = [a for a in self.agents if a.default]
        if len(defaults) != 1:
            raise ValueError("exactly one default orchestrator agent is required")
        ids = {a.id for a in self.agents}
        if len(ids) != len(self.agents):
            raise ValueError("duplicate agent ids are not allowed")
        for agent in self.agents:
            for tool in agent.tool_allow:
                if tool in GLOBAL_DENY_TOOLS:
                    raise ValueError(f"agent {agent.id} cannot allow globally denied tool {tool}")
            for denied in agent.tool_deny:
                if denied in agent.tool_allow:
                    raise ValueError(f"agent {agent.id} has conflicting allow/deny for {denied}")
        if not self.gateway.token:
            raise ValueError("gateway.token is required")
        if self.routing is not None:
            self._validate_routing()
        self._validate_implementation()
        self._validate_gate_models()

    def _validate_gate_models(self) -> None:
        by_id = {agent.id: agent for agent in self.agents}
        for agent_id in GATE_AGENT_IDS:
            agent = by_id.get(agent_id)
            if agent is None:
                continue
            allowed = list(self.policy.model_allowlist.get(agent_id) or [])
            if not allowed:
                raise ValueError(f"policy.model_allowlist.{agent_id} is required")
            if agent.model not in allowed:
                raise ValueError(f"agent {agent_id} model {agent.model} is not allowlisted")
            if agent.model in FORBIDDEN_GATE_MODELS or any(model in FORBIDDEN_GATE_MODELS for model in allowed):
                raise ValueError(f"agent {agent_id} cannot use gpt-4o-mini")

    def _validate_implementation(self) -> None:
        backend = str(self.implementation.coding_backend or "").strip()
        if not backend:
            return
        if backend == "opencode" and self.implementation.backends.opencode is None:
            raise ValueError("implementation.backends.opencode is required when coding_backend=opencode")

    def _validate_routing(self) -> None:
        assert self.routing is not None
        agent_ids = {agent.id for agent in self.agents}
        from ts_harness.workflows.materialize import list_skeleton_types

        workflow_types = set(list_skeleton_types())
        for rule in self.routing.intents:
            if rule.delegate_agent and rule.delegate_agent not in agent_ids:
                raise ValueError(f"routing intent {rule.id} references unknown agent {rule.delegate_agent}")
            if rule.workflow_type and rule.workflow_type not in workflow_types:
                raise ValueError(
                    f"routing intent {rule.id} references unknown workflow_type {rule.workflow_type}"
                )
            if rule.policy is not None:
                if rule.policy.mode not in {"require_workflow", "require_delegate"}:
                    raise ValueError(
                        f"routing intent {rule.id} policy.mode must be require_workflow or require_delegate"
                    )
                for denied_type in rule.policy.deny_start_workflow_types:
                    if denied_type not in workflow_types:
                        raise ValueError(
                            f"routing intent {rule.id} deny_start_workflow_types "
                            f"references unknown workflow_type {denied_type}"
                        )


def _parse_heartbeat(raw: dict[str, Any] | None) -> AgentHeartbeatConfig | None:
    if not raw:
        return None
    hours = raw.get("activeHours") or raw.get("active_hours") or {}
    return AgentHeartbeatConfig(
        every=str(raw.get("every", "30m")),
        model=raw.get("model"),
        target=str(raw.get("target", "none")),
        to=raw.get("to"),
        light_context=bool(raw.get("lightContext", raw.get("light_context", True))),
        isolated_session=bool(raw.get("isolatedSession", raw.get("isolated_session", True))),
        active_hours_start=str(hours.get("start", "07:00")),
        active_hours_end=str(hours.get("end", "22:00")),
        timezone=str(hours.get("timezone", "UTC")),
        prompt=str(raw.get("prompt", "Read HEARTBEAT.md. Reply HEARTBEAT_OK if nothing needs attention.")),
    )


def _parse_agent(raw: dict[str, Any]) -> AgentConfig:
    tools = raw.get("tools") or {}
    subagents = raw.get("subagents") or {}
    sandbox = raw.get("sandbox") or {}
    return AgentConfig(
        id=str(raw["id"]),
        workspace=str(raw.get("workspace", "")),
        model=str(raw.get("model", "openai/gpt-4o-mini")),
        skills=list(raw.get("skills") or []),
        tool_allow=list(tools.get("allow") or []),
        tool_deny=list(tools.get("deny") or []),
        toolsets=list(raw.get("toolsets") or []),
        connector_bindings={
            str(k): str(v) for k, v in (raw.get("connector_bindings") or {}).items()
        },
        default=bool(raw.get("default", False)),
        subagent_allow=list(subagents.get("allowAgents") or subagents.get("allow_agents") or []),
        heartbeat=_parse_heartbeat(raw.get("heartbeat")),
        sandbox_mode=str(sandbox.get("mode", "off")),
    )


def _parse_departments(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("identity.departments must be an object")
    from ts_harness.policy.permissions import DEPARTMENT_NAME_RE

    email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    departments: dict[str, str] = {}
    for raw_email, raw_name in raw.items():
        email = str(raw_email or "").strip().lower()
        name = str(raw_name or "").strip().lower()
        if not email_re.match(email):
            raise ValueError(f"identity.departments key is not an email: {raw_email}")
        if not DEPARTMENT_NAME_RE.fullmatch(name):
            raise ValueError(f"identity.departments.{email} is not a department name")
        departments[email] = name
    return departments


def _parse_identity(raw: dict[str, Any]) -> IdentityConfig:
    emails_raw = raw.get("slack_user_emails") or {}
    return IdentityConfig(
        provider=str(raw.get("provider", "static")),
        issuer=raw.get("issuer") or None,
        audience=raw.get("audience") or None,
        jwks_url=raw.get("jwks_url") or None,
        client_id=raw.get("client_id") or None,
        authorization_endpoint=raw.get("authorization_endpoint") or None,
        token_endpoint=raw.get("token_endpoint") or None,
        scim_enabled=bool(raw.get("scim_enabled", False)),
        slack_user_emails={str(k): str(v) for k, v in emails_raw.items()},
        jira_account_ids={
            str(k): str(v) for k, v in (raw.get("jira_account_ids") or {}).items()
        },
        github_logins={
            str(k).lower(): str(v) for k, v in (raw.get("github_logins") or {}).items()
        },
        roles={
            str(k).lower(): [str(role) for role in (v or [])]
            for k, v in (raw.get("roles") or {}).items()
            if isinstance(v, list)
        },
        departments=_parse_departments(raw.get("departments")),
        role_bundles={
            str(k).strip().lower(): [str(item).strip().lower() for item in (v or []) if str(item).strip()]
            for k, v in (raw.get("role_bundles") or {}).items()
            if isinstance(v, list)
        },
        service_permissions=(
            [str(item).strip().lower() for item in raw.get("service_permissions") if str(item).strip()]
            if isinstance(raw.get("service_permissions"), list)
            else None
        ),
        group_roles={
            str(k).strip(): [str(role).strip().lower() for role in (v or []) if str(role).strip()]
            for k, v in (raw.get("group_roles") or {}).items()
            if isinstance(v, list)
        },
        tenant_claim=str(raw.get("tenant_claim") or "tenant_id"),
        preset=_identity_choice(raw, "preset", IDENTITY_PRESETS),
        token_type=_identity_choice(raw, "token_type", ("access", "id")),
        groups_claim=_rendered(raw.get("groups_claim")),
        roles_claim=_rendered(raw.get("roles_claim")),
        groups_source=_identity_choice(raw, "groups_source", GROUPS_SOURCES),
        allowed_domains=_domain_list(raw.get("allowed_domains")),
        directory=_identity_choice(raw, "directory", DIRECTORY_SOURCES),
        team_groups=_parse_team_groups(raw.get("team_groups")),
    )


IDENTITY_PRESETS = ("auth0", "entra", "google", "okta", "generic")
GROUPS_SOURCES = ("token", "microsoft_graph", "google_directory")
DIRECTORY_SOURCES = ("scim", "auth0_management", "microsoft_graph", "google_directory", "seen_users")


def _rendered(value: Any) -> str | None:
    """Blank values and unrendered ``{{TEMPLATE}}`` placeholders mean unset."""
    text = str(value or "").strip()
    if not text or text.startswith("{{"):
        return None
    return text


def _identity_choice(raw: dict[str, Any], key: str, allowed: tuple[str, ...]) -> str | None:
    value = _rendered(raw.get(key))
    if value is None:
        return None
    value = value.lower()
    if value not in allowed:
        raise ValueError(f"identity.{key} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _domain_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [] if value.strip().startswith("{{") else value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        items = []
    return [str(item).strip().lower().lstrip("@") for item in items if str(item).strip()]


def _parse_team_groups(raw: Any) -> TeamGroupsConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = TeamGroupsConfig()
    return TeamGroupsConfig(
        prefix=str(raw.get("prefix") or defaults.prefix),
        approver_suffix=str(raw.get("approver_suffix") or defaults.approver_suffix),
        manager_suffix=str(raw.get("manager_suffix") or defaults.manager_suffix),
    )


def _parse_routing_policy(raw: dict[str, Any] | None) -> RoutingIntentPolicy | None:
    if not raw:
        return None
    return RoutingIntentPolicy(
        mode=str(raw.get("mode", "require_workflow")),
        deny_tools=[str(tool) for tool in raw.get("deny_tools") or []],
        deny_start_workflow_types=[
            str(workflow_type) for workflow_type in raw.get("deny_start_workflow_types") or []
        ],
        allow_program_types=[
            str(program_type) for program_type in raw.get("allow_program_types") or []
        ],
    )


def _parse_routing(raw: dict[str, Any] | None) -> RoutingConfig | None:
    if not raw:
        return None
    intents = [
        RoutingIntentRule(
            id=str(item["id"]),
            signals=[str(signal) for signal in item.get("signals") or []],
            exclude=[str(phrase) for phrase in item.get("exclude") or []],
            workflow_type=item.get("workflow_type"),
            program_type=item.get("program_type"),
            entry_phase=item.get("entry_phase"),
            delegate_agent=item.get("delegate_agent"),
            orchestrator_hint=item.get("orchestrator_hint"),
            policy=_parse_routing_policy(item.get("policy")),
        )
        for item in raw.get("intents") or []
    ]
    return RoutingConfig(intents=intents, default=str(raw.get("default", "ad_hoc_delegate")))


def _parse_google_workspace_connector(raw: dict[str, Any] | None) -> GoogleWorkspaceConnectorConfig | None:
    if not raw:
        return None
    auth_raw = raw.get("auth") or {}
    delegation_raw = auth_raw.get("delegation")
    oauth_raw = auth_raw.get("oauth")
    accounts: dict[str, GoogleWorkspaceAccountConfig] = {}
    for name, acct in (raw.get("accounts") or {}).items():
        accounts[str(name)] = GoogleWorkspaceAccountConfig(
            impersonate_email=str(acct.get("impersonate_email") or "")
        )
    publish_raw = raw.get("artifact_publish") or {}
    library_raw = publish_raw.get("library") or {}
    step_phase_map: dict[str, dict[str, str]] = {}
    for wf_type, mapping in (library_raw.get("step_phase_map") or {}).items():
        if isinstance(mapping, dict):
            step_phase_map[str(wf_type)] = {str(k): str(v) for k, v in mapping.items()}
    return GoogleWorkspaceConnectorConfig(
        enabled=bool(raw.get("enabled", True)),
        auth=GoogleWorkspaceConnectorAuth(
            delegation=GoogleWorkspaceDelegationAuth(
                credentials_secret=str(
                    (delegation_raw or {}).get("credentials_secret") or "google-workspace-sa-json"
                ),
                default_impersonate_email=str(
                    (delegation_raw or {}).get("default_impersonate_email") or ""
                ),
            )
            if delegation_raw is not False
            else None,
            oauth=GoogleWorkspaceOAuthAuth(
                client_id_secret=str((oauth_raw or {}).get("client_id_secret") or "google-oauth-client-id"),
                client_secret_secret=str(
                    (oauth_raw or {}).get("client_secret_secret") or "google-oauth-client-secret"
                ),
                callback_url=str((oauth_raw or {}).get("callback_url") or ""),
                kms_key_id=str((oauth_raw or {}).get("kms_key_id") or ""),
            )
            if oauth_raw
            else None,
        ),
        accounts=accounts,
        scopes=[str(s) for s in raw.get("scopes") or []],
        artifact_publish=GoogleWorkspaceArtifactPublishConfig(
            enabled=bool(publish_raw.get("enabled", True)),
            folder_id=str(publish_raw.get("folder_id") or ""),
            min_chars=int(publish_raw.get("min_chars", 3000)),
            chunk_chars=int(publish_raw.get("chunk_chars", 50000)),
            library=GoogleWorkspaceArtifactLibraryConfig(
                enabled=bool(library_raw.get("enabled", False)),
                publish_on=str(library_raw.get("publish_on") or "present"),
                share_folder_with_requester=bool(
                    library_raw.get("share_folder_with_requester", True)
                ),
                program_subfolder=str(library_raw.get("program_subfolder") or ""),
                step_phase_map=step_phase_map,
            ),
        ),
    )


def _parse_connectors(data: dict[str, Any]) -> ConnectorsConfig:
    connectors_raw = data.get("connectors") or {}
    return ConnectorsConfig(
        google_workspace=_parse_google_workspace_connector(connectors_raw.get("google-workspace")),
    )


def _parse_implementation(raw: dict[str, Any] | None) -> ImplementationConfig:
    if not raw:
        return ImplementationConfig()
    backends_raw = raw.get("backends") or {}
    opencode_raw = backends_raw.get("opencode") or {}
    run_args = opencode_raw.get("run_args")
    if not isinstance(run_args, list) or not run_args:
        run_args = ["run", "--auto"]
    repos: dict[str, ImplementationRepoConfig] = {}
    for name, repo in (raw.get("repos") or {}).items():
        if not isinstance(repo, dict):
            continue
        repos[str(name)] = ImplementationRepoConfig(
            url=str(repo.get("url") or ""),
            base_branch=str(repo.get("base_branch") or "main"),
            clone_depth=int(repo.get("clone_depth") or 1),
        )
    return ImplementationConfig(
        coding_backend=str(raw.get("coding_backend") or "opencode"),
        tools_root=str(raw.get("tools_root") or ""),
        backends=ImplementationBackendsConfig(
            opencode=OpenCodeBackendConfig(
                version=str(opencode_raw.get("version") or "latest"),
                binary=str(opencode_raw.get("binary") or "opencode"),
                provider=str(opencode_raw.get("provider") or ""),
                auth_env=str(opencode_raw.get("auth_env") or "CODING_BACKEND_API_KEY"),
                model=str(opencode_raw.get("model") or ""),
                run_args=[str(arg) for arg in run_args],
                verification_fix_passes=max(
                    0,
                    int(
                        opencode_raw["verification_fix_passes"]
                        if "verification_fix_passes" in opencode_raw
                        else 2
                    ),
                ),
            )
        ),
        repos=repos,
    )


def parse_runtime_config(data: dict[str, Any]) -> RuntimeConfig:
    tenants = [
        TenantConfig(
            id=str(t["id"]),
            display_name=str(t.get("display_name", t["id"])),
            enabled_connectors=list(t.get("enabled_connectors") or []),
            data_residency=str(t.get("data_residency", "us")),
        )
        for t in data.get("tenants") or [{"id": "default", "display_name": "Default"}]
    ]
    agents = [_parse_agent(a) for a in data.get("agents", {}).get("list", data.get("agents_list", []))]
    gateway_raw = data.get("gateway") or {}
    subagent_raw = data.get("agents", {}).get("defaults", {}).get("subagents", {})
    config = RuntimeConfig(
        tenants=tenants,
        agents=agents,
        gateway=GatewayConfig(
            token=str(gateway_raw.get("token", "")),
            allowed_origins=list(gateway_raw.get("allowed_origins") or []),
            single_tenant_mode=bool(gateway_raw.get("single_tenant_mode", True)),
            default_tenant_id=str(gateway_raw.get("default_tenant_id", "default")),
        ),
        sandbox=SandboxConfig(**{k: v for k, v in (data.get("sandbox") or {}).items() if k in SandboxConfig.__dataclass_fields__}),
        policy=PolicyConfig(**{k: v for k, v in (data.get("policy") or {}).items() if k in PolicyConfig.__dataclass_fields__}),
        subagents=SubagentLimitsConfig(
            max_depth=int(subagent_raw.get("maxSpawnDepth", subagent_raw.get("max_depth", 2))),
            max_children_per_agent=int(
                subagent_raw.get("maxChildrenPerAgent", subagent_raw.get("max_children_per_agent", 5))
            ),
            max_concurrent=int(subagent_raw.get("maxConcurrent", subagent_raw.get("max_concurrent", 8))),
        ),
        identity=_parse_identity(data.get("identity") or {}),
        bindings=dict(data.get("bindings") or {}),
        toolsets={k: list(v) for k, v in (data.get("toolsets") or {}).items()},
        mcp_servers=dict(data.get("mcp_servers") or {}),
        connectors=_parse_connectors(data),
        routing=_parse_routing(data.get("routing")),
        implementation=_parse_implementation(data.get("implementation")),
        program_defaults=dict(data.get("program_defaults") or {}),
        program_builder=ProgramBuilderConfig(
            agent_id=str((data.get("program_builder") or {}).get("agent_id") or "builder-agent"),
        ),
        deployment=_parse_deployment(data.get("deployment")),
        scheduling=parse_scheduling(data.get("scheduling")),
    )
    config.validate()
    config._last_known_good = copy.deepcopy(data)
    from ts_harness.config.agents_overlay import fill_agent_souls
    from ts_harness.config.runtime_settings import _publish_sandbox_limits

    fill_agent_souls(config.agents)
    _publish_sandbox_limits(config)
    return config


def parse_scheduling(raw: Any) -> SchedulingConfig:
    raw = raw if isinstance(raw, dict) else {}
    coding = raw.get("coding") if isinstance(raw.get("coding"), dict) else {}
    defaults = SchedulingConfig()

    def count(key: str, default: int | None) -> int | None:
        value = coding.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"scheduling.coding.{key} must be a non-negative integer")
        return value

    return SchedulingConfig(
        coding_per_user=count("per_user", defaults.coding_per_user) or 0,
        coding_per_program=count("per_program", defaults.coding_per_program) or 0,
        coding_per_team=count("per_team", defaults.coding_per_team) or 0,
        coding_global=count("global", None),
    )


def _parse_deployment(raw: Any) -> DeploymentConfig:
    from ts_harness.config.deployment_profile import normalize_profile

    raw = raw if isinstance(raw, dict) else {}
    overrides = raw.get("overrides")
    return DeploymentConfig(
        profile=normalize_profile(raw.get("profile")),
        overrides=dict(overrides) if isinstance(overrides, dict) else {},
    )


def _substitute_templates(text: str) -> str:
    import os
    import re

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return os.environ.get(key, match.group(0))

    return re.sub(r"\{\{([A-Z0-9_]+)\}\}", repl, text)


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    path = Path(path)
    text = _substitute_templates(path.read_text())
    if path.suffix in {".yaml", ".yml"} or path.name.endswith((".yaml.tmpl", ".yml.tmpl")):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    gateway = data.get("gateway") or {}
    if not gateway.get("token") or str(gateway.get("token")).startswith("{{"):
        import os

        gateway["token"] = os.environ.get("GATEWAY_TOKEN", "")
        data["gateway"] = gateway
    return parse_runtime_config(data)
