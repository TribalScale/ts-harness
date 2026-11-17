"""Admin view of the GitHub App: registration, installations, webhooks and rate limits."""

from __future__ import annotations

import os
from typing import Any, Callable

from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.projects_bff import ProjectAccessDenied, _is_admin
from ts_harness.integrations import github_app
from ts_harness.integrations.github_client import RATE_LIMITS
from ts_harness.observability.logging import log_event


def _install_url(host: str, slug: str) -> str:
    if not slug:
        return ""
    return f"https://{host}/apps/{slug}/installations/new"


def _registration_payload(host: str, row: dict[str, Any] | None, registration: Any) -> dict[str, Any]:
    slug = getattr(registration, "app_slug", "") or (row or {}).get("app_slug") or ""
    return {
        "host": host,
        "configured": registration is not None,
        "app_id": getattr(registration, "app_id", "") or (row or {}).get("app_id") or "",
        "app_slug": slug,
        "source": "environment" if github_app._env_registration(host) else ("stored" if row else ""),
        "webhook_secret": bool(getattr(registration, "webhook_secret", "")),
        "install_url": _install_url(host, slug),
    }


def _installation_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": row.get("host"),
        "installation_id": row.get("installation_id"),
        "account_login": row.get("account_login"),
        "account_type": row.get("account_type"),
        "repository_selection": row.get("repository_selection"),
        "suspended": bool(row.get("suspended_at")),
        "repositories": list(row.get("repositories") or []),
        "synced_at": row.get("synced_at"),
    }


def github_overview(memory: Any, auth: AuthContext, config: Any, _body: dict[str, Any] | None = None) -> dict[str, Any]:
    default_host = (os.environ.get("GITHUB_APP_HOST") or github_app.DEFAULT_HOST).strip().lower()
    stored = {str(r.get("host")): r for r in memory.list_github_registrations()}
    hosts = sorted({default_host, *stored})
    apps = [_registration_payload(h, stored.get(h), github_app.registration_for(h)) for h in hosts]
    return {
        "apps": apps,
        "installations": [_installation_payload(r) for r in memory.list_github_installations()],
        "webhooks": memory.github_webhook_status(),
        "rate_limits": [{"host": h, **v} for h, v in sorted(RATE_LIMITS.items())],
        "pat_fallback": bool(os.environ.get("GITHUB_TOKEN", "").strip()),
        "can_sync": _is_admin(auth, config),
    }


def sync_installations(
    memory: Any,
    auth: AuthContext,
    config: Any,
    body: dict[str, Any] | None = None,
    *,
    app_for: Callable[[str], Any] = github_app.app_for,
) -> dict[str, Any]:
    """Reload installations and their repositories from GitHub, for missed webhooks."""
    if not _is_admin(auth, config):
        raise ProjectAccessDenied("only admins can sync GitHub installations")
    host = str((body or {}).get("host") or os.environ.get("GITHUB_APP_HOST") or github_app.DEFAULT_HOST).strip().lower()
    app = app_for(host)
    if app is None:
        raise ValueError(f"no GitHub App is configured for {host}")
    seen: set[int] = set()
    for item in app.list_installations():
        installation_id = int(item["id"])
        seen.add(installation_id)
        account = item.get("account") or {}
        memory.upsert_github_installation(
            host=host,
            installation_id=installation_id,
            account_login=str(account.get("login") or ""),
            account_type=str(account.get("type") or ""),
            repository_selection=str(item.get("repository_selection") or ""),
            suspended=bool(item.get("suspended_at")),
            repositories=app.installation_repositories(installation_id),
            actor=auth.actor_id,
        )
    removed = 0
    for row in memory.list_github_installations(host=host):
        if int(row["installation_id"]) not in seen:
            memory.delete_github_installation(host, int(row["installation_id"]))
            removed += 1
    github_app.reset_cache()
    log_event("github.installations_synced", host=host, installations=len(seen), removed=removed, actor=auth.actor_id)
    return {"host": host, "installations": len(seen), "removed": removed}
