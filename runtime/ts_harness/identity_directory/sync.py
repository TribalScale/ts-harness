"""Choose the configured directory source, sync it, and register sign-in group resolvers."""

from __future__ import annotations

from typing import Any

from ts_harness.identity_directory.auth0_management import Auth0ManagementSource
from ts_harness.identity_directory.base import DirectorySource
from ts_harness.identity_directory.google_directory import GoogleDirectorySource
from ts_harness.identity_directory.microsoft_graph import MicrosoftGraphSource
from ts_harness.identity_directory.scim import ScimSource
from ts_harness.identity_directory.seen_users import SeenUsersSource
from ts_harness.observability.logging import log_event

GROUP_RESOLVER_SOURCES = {"microsoft_graph": MicrosoftGraphSource, "google_directory": GoogleDirectorySource}


def _config(config: Any) -> Any:
    if config is not None:
        return config
    from ts_harness.gateway.server import get_runtime

    return get_runtime().config


def _tenant(config: Any) -> str:
    return str(getattr(getattr(config, "gateway", None), "default_tenant_id", None) or "default")


def _setting(config: Any, name: str) -> str:
    from ts_harness.gateway.identity_presets import identity_setting

    return identity_setting(getattr(config, "identity", None), name)


def _group_prefix(config: Any) -> str:
    team_groups = getattr(getattr(config, "identity", None), "team_groups", None)
    prefix = str(getattr(team_groups, "prefix", "") or "ts-harness-team-")
    return prefix.split("team-", 1)[0] or "ts-harness-"


def build_source(config: Any, *, memory: Any = None) -> DirectorySource | None:
    """The configured source, or None when it is not set up (missing credentials)."""
    name = _setting(config, "directory")
    if name == "seen_users":
        return SeenUsersSource()
    if name == "scim":
        if memory is None:
            from ts_harness.memory.client import get_memory_client

            memory = get_memory_client()
        return ScimSource(memory=memory, tenant_id=_tenant(config))
    factories = {
        "auth0_management": Auth0ManagementSource,
        "microsoft_graph": MicrosoftGraphSource,
        "google_directory": GoogleDirectorySource,
    }
    factory = factories.get(name)
    if factory is None:
        return None
    return factory.from_env(config, group_prefix=_group_prefix(config))


def sync_directory(config: Any = None, *, memory: Any = None, source: DirectorySource | None = None) -> dict[str, Any]:
    from ts_harness.gateway import directory_links
    from ts_harness.policy.permissions import resolve_actor_roles, resolve_team_memberships

    config = _config(config)
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    source = source or build_source(config, memory=memory)
    if source is None:
        return {"status": "skipped", "reason": "directory source is not configured"}
    snapshot = source.fetch()
    if not snapshot.people and not snapshot.complete:
        return {"status": "ok", "source": snapshot.source, "people": 0}
    users: list[dict[str, Any]] = []
    for person in snapshot.people:
        entry: dict[str, Any] = {
            "subject": person.subject,
            "email": person.email,
            "display_name": person.display_name,
            "disabled": person.disabled,
        }
        if person.github_login:
            entry["github_login"] = person.github_login
        if person.groups is not None:
            entry["teams"] = resolve_team_memberships(person.groups, config)
        if person.roles is not None or person.groups is not None:
            entry["roles"] = resolve_actor_roles(
                claim_roles=person.roles or [], groups=person.groups or [], email=person.email, config=config
            )
        users.append(entry)
    result = memory.directory_snapshot(
        tenant_id=_tenant(config),
        source=snapshot.source,
        match=snapshot.match,
        complete=snapshot.complete,
        users=users,
    )
    directory_links.clear_cache()
    log_event(
        "directory_sync",
        source=snapshot.source,
        people=len(users),
        disabled=len(result.get("disabled") or []),
        disable_skipped=bool(result.get("disable_skipped")),
    )
    if result.get("disable_skipped"):
        log_event("directory_sync_disable_skipped", source=snapshot.source, would_disable=result.get("would_disable"))
    return {"status": "ok", "people": len(users), **result}


def register_group_resolvers(config: Any) -> str | None:
    """Register the sign-in group resolver for ``identity.groups_source``; returns its name."""
    from ts_harness.gateway.identity import register_group_resolver

    name = _setting(config, "groups_source")
    factory = GROUP_RESOLVER_SOURCES.get(name)
    if factory is None:
        return None
    resolver = factory.from_env(config, group_prefix=_group_prefix(config))
    if resolver is None:
        log_event("group_resolver_unconfigured", source=name)
        return None
    register_group_resolver(name, resolver)
    return name
