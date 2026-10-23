"""Admin BFF for projects and their package registries.

Admins manage every project; a team manager manages the projects their team
owns. Registry credentials are write-only: they go to the cloud secret store
under ``{SECRETS_PREFIX}/projects/{project}/registries/{registry}`` and are
never returned. Saving or deleting a registry refreshes the egress proxy
allowlist (scale_out) so workers can reach the new host.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from ts_harness.gateway.auth import AuthContext
from ts_harness.observability.logging import log_event


class ProjectAccessDenied(PermissionError):
    pass


class SecretStore(Protocol):
    def put(self, name: str, payload: dict[str, str]) -> str: ...

    def get(self, ref: str) -> dict[str, str]: ...

    def delete(self, ref: str) -> None: ...


class UnavailableSecretStore:
    def _fail(self) -> None:
        raise RuntimeError("registry credentials need AWS Secrets Manager (CLOUD_PROVIDER=aws)")

    def put(self, name: str, payload: dict[str, str]) -> str:
        self._fail()
        return ""

    def get(self, ref: str) -> dict[str, str]:
        self._fail()
        return {}

    def delete(self, ref: str) -> None:
        self._fail()


class AwsSecretStore:
    def __init__(self, *, region: str | None = None, client: Any = None):
        self.region = region
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("secretsmanager", region_name=self.region or None)
        return self._client

    def put(self, name: str, payload: dict[str, str]) -> str:
        value = json.dumps(payload)
        try:
            return str(self.client.create_secret(Name=name, SecretString=value)["ARN"])
        except self.client.exceptions.ResourceExistsException:
            return str(self.client.put_secret_value(SecretId=name, SecretString=value)["ARN"])

    def get(self, ref: str) -> dict[str, str]:
        raw = str(self.client.get_secret_value(SecretId=ref).get("SecretString") or "")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"token": raw}
        return payload if isinstance(payload, dict) else {"token": raw}

    def delete(self, ref: str) -> None:
        self.client.delete_secret(SecretId=ref, RecoveryWindowInDays=7)


def default_secret_store() -> SecretStore:
    if os.environ.get("CLOUD_PROVIDER", "aws").strip().lower() != "aws":
        return UnavailableSecretStore()
    return AwsSecretStore(region=os.environ.get("CLOUD_REGION") or os.environ.get("AWS_REGION"))


def secret_name(project_id: str, registry_id: str) -> str:
    prefix = (os.environ.get("SECRETS_PREFIX") or "ts-harness").strip("/")
    return f"{prefix}/projects/{project_id}/registries/{registry_id}"


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _is_admin(auth: AuthContext, config: Any) -> bool:
    if getattr(auth, "is_service", False):
        return True
    from ts_harness.policy.permissions import actor_permissions

    return "edit_config" in actor_permissions(auth, config)


def _managed_teams(auth: AuthContext) -> set[str]:
    return {str(t.get("slug")) for t in (getattr(auth, "teams", None) or []) if t.get("role") == "manager"}


def can_manage(auth: AuthContext, project: dict[str, Any], config: Any) -> bool:
    return _is_admin(auth, config) or (bool(project.get("team_slug")) and project["team_slug"] in _managed_teams(auth))


def _require_manage(auth: AuthContext, project: dict[str, Any], config: Any) -> None:
    if not can_manage(auth, project, config):
        raise ProjectAccessDenied("only admins and managers of the owning team can change this project")


def _tenant(auth: AuthContext) -> str:
    return str(getattr(auth, "tenant_id", "") or "default")


def _actor(auth: AuthContext) -> str:
    return str(getattr(auth, "actor_id", "") or "admin")


def _project(memory: Any, auth: AuthContext, project_id: str) -> dict[str, Any]:
    project = memory.get_project(project_id)
    if project.get("tenant_id") != _tenant(auth):
        raise LookupError("project not found")
    return project


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def list_projects_admin(memory: Any, auth: AuthContext, config: Any) -> dict[str, Any]:
    projects = memory.list_projects(tenant_id=_tenant(auth)).get("projects") or []
    return {
        "projects": [{**p, "can_manage": can_manage(auth, p, config)} for p in projects],
        "is_admin": _is_admin(auth, config),
    }


def get_project_admin(memory: Any, auth: AuthContext, config: Any, project_id: str) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    return {**project, "can_manage": can_manage(auth, project, config)}


def create_project_admin(memory: Any, auth: AuthContext, config: Any, body: dict[str, Any]) -> dict[str, Any]:
    if not _is_admin(auth, config):
        raise ProjectAccessDenied("only admins can create projects")
    fields = {k: body.get(k) for k in ("key", "name", "team_slug", "jira_project_key", "default_base_branch",
                                         "worker_cpu", "worker_memory_mb") if k in body}
    project = memory.create_project(tenant_id=_tenant(auth), actor=_actor(auth), **fields)
    return {**project, "can_manage": True}


def update_project_admin(
    memory: Any, auth: AuthContext, config: Any, project_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    if "team_slug" in body and not _is_admin(auth, config):
        raise ProjectAccessDenied("only admins can move a project to another team")
    fields = {k: body[k] for k in ("name", "team_slug", "jira_project_key", "default_base_branch",
                                   "worker_cpu", "worker_memory_mb") if k in body}
    updated = memory.update_project(project["id"], actor=_actor(auth), **fields)
    return {**updated, "can_manage": can_manage(auth, updated, config)}


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


def _public_registry(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "secret_arn"}


def _credential(body: dict[str, Any]) -> dict[str, str] | None:
    token = str(body.get("token") or "")
    if not token:
        return None
    payload = {"token": token}
    if str(body.get("username") or "").strip():
        payload["username"] = str(body["username"]).strip()
    return payload


def list_registries_admin(memory: Any, auth: AuthContext, config: Any, project_id: str) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    rows = memory.list_project_registries(project["id"]).get("registries") or []
    return {"registries": [_public_registry(r) for r in rows]}


def create_registry_admin(
    memory: Any,
    auth: AuthContext,
    config: Any,
    project_id: str,
    body: dict[str, Any],
    *,
    secrets: SecretStore | None = None,
    sync: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    credential = _credential(body)
    store = secrets or default_secret_store()
    registry = memory.create_project_registry(
        project["id"],
        type=body.get("type"),
        url=body.get("url"),
        scope=body.get("scope"),
        username=body.get("username"),
        actor=_actor(auth),
    )
    if credential:
        try:
            arn = store.put(secret_name(project["id"], registry["id"]), credential)
        except Exception:
            memory.delete_project_registry(project["id"], registry["id"], actor=_actor(auth))
            raise
        registry = memory.update_project_registry(project["id"], registry["id"], secret_arn=arn, actor=_actor(auth))
    allowlist = (sync or sync_egress_allowlist)(memory)
    return {**_public_registry(registry), "egress": allowlist}


def update_registry_admin(
    memory: Any,
    auth: AuthContext,
    config: Any,
    project_id: str,
    registry_id: str,
    body: dict[str, Any],
    *,
    secrets: SecretStore | None = None,
    sync: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    fields = {k: body[k] for k in ("url", "scope", "username") if k in body}
    credential = _credential(body)
    if credential:
        store = secrets or default_secret_store()
        fields["secret_arn"] = store.put(secret_name(project["id"], registry_id), credential)
    elif body.get("clear_credential") is True:
        current = next(
            (r for r in memory.list_project_registries(project["id"]).get("registries") or [] if r["id"] == registry_id),
            None,
        )
        if current and current.get("secret_arn"):
            (secrets or default_secret_store()).delete(current["secret_arn"])
        fields["secret_arn"] = None
    registry = memory.update_project_registry(project["id"], registry_id, actor=_actor(auth), **fields)
    allowlist = (sync or sync_egress_allowlist)(memory) if "url" in fields else None
    return {**_public_registry(registry), "egress": allowlist}


def delete_registry_admin(
    memory: Any,
    auth: AuthContext,
    config: Any,
    project_id: str,
    registry_id: str,
    *,
    secrets: SecretStore | None = None,
    sync: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    registry = memory.delete_project_registry(project["id"], registry_id, actor=_actor(auth))
    if registry.get("secret_arn"):
        try:
            (secrets or default_secret_store()).delete(registry["secret_arn"])
        except Exception as exc:  # noqa: BLE001
            log_event("registry_secret_delete_failed", registry_id=registry_id, error=str(exc)[:200])
    allowlist = (sync or sync_egress_allowlist)(memory)
    return {**_public_registry(registry), "deleted": True, "egress": allowlist}


def list_repositories_admin(memory: Any, auth: AuthContext, config: Any, project_id: str) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    return {"repositories": list(memory.list_project_repositories(project["id"]).get("repositories") or [])}


def _installation_for(memory: Any, host: str, full_name: str) -> int | None:
    """The App installation that covers ``full_name``; ``ValueError`` when installations exist but none does."""
    try:
        installations = memory.list_github_installations(host=host)
    except Exception:  # noqa: BLE001
        return None
    if not installations:
        return None
    wanted = full_name.lower()
    for installation in installations:
        if wanted in {str(r).lower() for r in installation.get("repositories") or []}:
            return int(installation["installation_id"])
    raise ValueError(f"the GitHub App is not installed on {full_name}; install it from System > GitHub first")


def create_repository_admin(
    memory: Any, auth: AuthContext, config: Any, project_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    host = str(body.get("github_host") or "github.com").strip().lower()
    full_name = str(body.get("full_name") or f"{body.get('owner') or ''}/{body.get('repo') or ''}").strip()
    return memory.create_project_repository(
        project["id"],
        github_host=host,
        full_name=full_name,
        installation_id=_installation_for(memory, host, full_name),
        default_branch=body.get("default_branch"),
        role=body.get("role"),
        actor=_actor(auth),
    )


def update_repository_admin(
    memory: Any, auth: AuthContext, config: Any, project_id: str, repository_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    fields = {k: body[k] for k in ("default_branch", "role") if k in body}
    return memory.update_project_repository(project["id"], repository_id, actor=_actor(auth), **fields)


def delete_repository_admin(
    memory: Any, auth: AuthContext, config: Any, project_id: str, repository_id: str
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    return {**memory.delete_project_repository(project["id"], repository_id, actor=_actor(auth)), "deleted": True}


# ---------------------------------------------------------------------------
# Registry test: one authenticated request through the egress proxy
# ---------------------------------------------------------------------------


def registry_test_url(registry: dict[str, Any]) -> str:
    url = str(registry["url"]).rstrip("/")
    kind = registry["type"]
    if kind == "npm":
        return f"{url}/-/whoami"
    if kind == "pypi":
        return url if url.endswith("/simple") else f"{url}/"
    if kind == "docker":
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/v2/"
    return f"{url}/"


def _test_request(registry: dict[str, Any], credential: dict[str, str] | None, *, proxy: str | None) -> tuple[bool, str]:
    import httpx

    headers: dict[str, str] = {}
    auth = None
    if credential and credential.get("token"):
        if credential.get("username") or registry["type"] in {"pypi", "maven", "docker"}:
            auth = (credential.get("username") or "token", credential["token"])
        else:
            headers["Authorization"] = f"Bearer {credential['token']}"
    try:
        with httpx.Client(proxy=proxy or None, timeout=15, follow_redirects=True) as client:
            response = client.get(registry_test_url(registry), headers=headers, auth=auth)
    except httpx.HTTPError as exc:
        return False, f"request failed: {type(exc).__name__}"
    if response.status_code < 400:
        return True, ""
    return False, f"HTTP {response.status_code}"


def test_registry_admin(
    memory: Any,
    auth: AuthContext,
    config: Any,
    project_id: str,
    registry_id: str,
    *,
    secrets: SecretStore | None = None,
    request: Callable[..., tuple[bool, str]] = _test_request,
) -> dict[str, Any]:
    project = _project(memory, auth, project_id)
    _require_manage(auth, project, config)
    registry = next(
        (r for r in memory.list_project_registries(project["id"]).get("registries") or [] if r["id"] == registry_id),
        None,
    )
    if registry is None:
        raise LookupError("registry not found")
    credential = (secrets or default_secret_store()).get(registry["secret_arn"]) if registry.get("secret_arn") else None
    ok, error = request(registry, credential, proxy=os.environ.get("CODING_EGRESS_PROXY_URL") or None)
    updated = memory.record_project_registry_test(project["id"], registry_id, ok=ok, error=error, actor=_actor(auth))
    return _public_registry(updated)


# ---------------------------------------------------------------------------
# Egress allowlist (scale_out): SSM parameter read by the proxy on start
# ---------------------------------------------------------------------------


def sync_egress_allowlist(memory: Any, *, ssm: Any = None, ecs: Any = None) -> dict[str, Any]:
    parameter = os.environ.get("CODING_EGRESS_DOMAINS_PARAMETER", "").strip()
    if not parameter:
        return {"managed": False}
    hosts = memory.registry_hosts()
    value = ",".join(hosts) or "none"
    region = os.environ.get("CLOUD_REGION") or os.environ.get("AWS_REGION") or None
    if ssm is None or ecs is None:
        import boto3

        ssm = ssm or boto3.client("ssm", region_name=region)
        ecs = ecs or boto3.client("ecs", region_name=region)
    try:
        current = str(ssm.get_parameter(Name=parameter)["Parameter"]["Value"])
    except Exception:  # noqa: BLE001
        current = ""
    if current == value:
        return {"managed": True, "hosts": hosts, "changed": False}
    ssm.put_parameter(Name=parameter, Value=value, Type="String", Overwrite=True)
    cluster = os.environ.get("CODING_ECS_CLUSTER", "").strip()
    service = os.environ.get("CODING_EGRESS_PROXY_SERVICE", "").strip()
    restarted = False
    if cluster and service:
        ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
        restarted = True
    log_event("egress_allowlist_updated", hosts=len(hosts), restarted=restarted)
    return {"managed": True, "hosts": hosts, "changed": True, "proxy_restarted": restarted}
