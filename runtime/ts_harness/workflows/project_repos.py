"""The repositories of a program's project, carried in program artifacts as ``project_repositories``."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_FIELDS = ("full_name", "repo_url", "github_host", "default_branch", "role")


def project_repositories(client: Any, *, tenant_id: str, project_key: str | None) -> list[dict[str, Any]]:
    """Repositories registered on the project whose key is ``project_key``; empty when there are none."""
    key = str(project_key or "").strip()
    if not key:
        return []
    try:
        projects = client.list_projects(tenant_id=tenant_id or "default", key=key)
        rows = projects.get("projects") if isinstance(projects, dict) else None
        if not isinstance(rows, list) or not rows:
            return []
        listed = client.list_project_repositories(str(rows[0]["id"]))
        repositories = listed.get("repositories") if isinstance(listed, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.info("project repositories for %s unavailable: %s", key, exc)
        return []
    if not isinstance(repositories, list):
        return []
    # The project's testing setting travels with each repository (bdd_policy decides per repository).
    bdd_mode = str(rows[0].get("bdd_mode") or "auto")
    return [
        {**{field: r.get(field) for field in _FIELDS}, "bdd_mode": bdd_mode}
        for r in repositories
        if isinstance(r, dict) and r.get("full_name")
    ]


def with_project_repositories(
    client: Any, artifacts: dict[str, Any], *, tenant_id: str, project_key: str | None
) -> dict[str, Any]:
    """``artifacts`` with the project's current repositories, when it has any."""
    repositories = project_repositories(client, tenant_id=tenant_id, project_key=project_key)
    if not repositories:
        return artifacts
    previous = [r for r in artifacts.get("project_repositories") or [] if isinstance(r, dict)]
    return {**artifacts, "project_repositories": _annotated(repositories, previous)}


def _annotated(repositories: list[dict[str, Any]], previous: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Template, CI and BDD facts per repository (repo_bootstrap.annotate_repositories).

    ``previous`` is what the program already holds: its BDD decisions are kept.
    """
    try:
        from ts_harness.integrations.github_client import github_for
        from ts_harness.workflows.repo_bootstrap import annotate_repositories

        return annotate_repositories(github_for(None), repositories, previous=previous)
    except Exception as exc:  # noqa: BLE001 - planning goes on without the annotation
        logger.info("repository annotation skipped: %s", exc)
        kept = {str(r.get("full_name") or "").lower(): r for r in previous}
        return [{**r, **{k: v for k, v in kept.get(str(r.get("full_name") or "").lower(), {}).items() if k in ("bdd", "bdd_reason")}} for r in repositories]
