"""Tenant-scoped Jira and GitHub bindings enforced in code, not prompts."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
from urllib.parse import urlparse

_ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]+)-\d+$")
_JIRA_WRITE_MARKERS = (
    "create_issue",
    "batch_create",
    "update_issue",
    "delete_issue",
    "transition_issue",
    "add_comment",
    "edit_comment",
    "add_worklog",
    "create_issue_link",
    "remove_issue_link",
    "link_to_epic",
    "create_remote_issue_link",
    "assign_issue",
    "move_issue",
    "create_version",
    "batch_create_versions",
    "update_version",
    "create_sprint",
    "update_sprint",
    "add_issues_to_sprint",
    "create_customer_request",
)


@dataclass(frozen=True)
class TenantBindings:
    github_org: str = ""
    allowed_repos: tuple[str, ...] = field(default_factory=tuple)
    jira_allowed_projects: tuple[str, ...] = field(default_factory=tuple)


def tenant_bindings(program_defaults: dict[str, Any] | None) -> TenantBindings:
    defaults = program_defaults if isinstance(program_defaults, dict) else {}
    github = defaults.get("github") if isinstance(defaults.get("github"), dict) else {}
    picker = defaults.get("repo_picker") if isinstance(defaults.get("repo_picker"), dict) else {}
    filing = defaults.get("jira_filing") if isinstance(defaults.get("jira_filing"), dict) else {}
    repos = tuple(
        str(item.get("id") or "").strip().lower()
        for item in (picker.get("repos") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    )
    projects = tuple(
        str(key).strip().upper()
        for key in (filing.get("allowed_projects") or [])
        if str(key or "").strip()
    )
    return TenantBindings(
        github_org=str(github.get("org") or "").strip(),
        allowed_repos=repos,
        jira_allowed_projects=projects,
    )


def github_repo_slug(repo_url: str | None) -> str | None:
    """Return ``org/repo`` for a GitHub https/ssh URL or bare slug."""
    raw = str(repo_url or "").strip()
    if not raw:
        return None
    if raw.startswith("git@"):
        _, _, path = raw.partition(":")
    elif "://" in raw:
        parsed = urlparse(raw)
        if (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}:
            return None
        path = parsed.path
    else:
        path = raw
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return f"{parts[0]}/{repo}".lower()


def check_repo_allowed(repo_url: str | None, bindings: TenantBindings) -> str | None:
    """Return an error message when the repo is outside the tenant binding."""
    if not bindings.github_org and not bindings.allowed_repos:
        return None
    slug = github_repo_slug(repo_url)
    if not slug:
        return f"repo is not a GitHub repository: {repo_url}"
    org = slug.split("/", 1)[0]
    if bindings.github_org and org != bindings.github_org.lower():
        return f"repo {slug} is outside the tenant GitHub org {bindings.github_org}"
    if bindings.allowed_repos and slug not in bindings.allowed_repos:
        return f"repo {slug} is not on the tenant repo allowlist"
    return None


def is_jira_write_tool(tool_name: str) -> bool:
    name = str(tool_name or "").lower()
    return "jira" in name and any(marker in name for marker in _JIRA_WRITE_MARKERS)


def _project_from_issue_key(value: Any) -> str | None:
    match = _ISSUE_KEY_RE.match(str(value or "").strip().upper())
    return match.group(1) if match else None


def _collect(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"project_key", "project"} and isinstance(item, str) and item.strip():
                out.add(item.strip().upper())
            elif lowered == "project" and isinstance(item, dict) and item.get("key"):
                out.add(str(item["key"]).strip().upper())
            elif lowered in {"issue_key", "issue_id", "parent_key", "epic_key", "inward_issue_key", "outward_issue_key"}:
                project = _project_from_issue_key(item)
                if project:
                    out.add(project)
            else:
                _collect(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect(item, out)


def jira_project_keys_from_args(args: dict[str, Any] | None) -> set[str]:
    out: set[str] = set()
    _collect(args or {}, out)
    return out


def check_jira_projects_allowed(project_keys: set[str], bindings: TenantBindings) -> str | None:
    if not bindings.jira_allowed_projects:
        return None
    outside = sorted(key for key in project_keys if key not in bindings.jira_allowed_projects)
    if outside:
        return (
            "Jira project(s) outside the tenant binding: "
            + ", ".join(outside)
            + f" (allowed: {', '.join(bindings.jira_allowed_projects)})"
        )
    return None
