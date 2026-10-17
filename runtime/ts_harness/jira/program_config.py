"""Merge program snapshot config with runtime defaults."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ts_harness.jira.pointer_block import DEFAULT_BASE_BRANCH
from ts_harness.jira.ticket_query import DEFAULT_TICKET_STATUSES

DEFAULT_ISSUE_TYPE = "Story"
CONFIG_SECTIONS = ("repo_picker", "jira_filing", "ticket_picker")
VALID_MODES = {"inherit", "custom"}


def default_program_runtime_config() -> dict[str, Any]:
    return {
        "repo_picker": {"repos": []},
        "jira_filing": {
            "default_issue_type": DEFAULT_ISSUE_TYPE,
            "issue_type_by_project": {},
        },
        "ticket_picker": {
            "assignee": "mapped_user",
            "statuses": list(DEFAULT_TICKET_STATUSES),
        },
    }


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _section_mode(section: Any) -> str | None:
    if not isinstance(section, dict):
        return None
    mode = str(section.get("mode") or "").strip().lower()
    return mode or None


def _has_nonempty_repos(section: dict[str, Any]) -> bool:
    repos = section.get("repos")
    if not isinstance(repos, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("id") or "").strip() for item in repos
    )


def _has_nonempty_filing_override(section: dict[str, Any]) -> bool:
    default_type = str(section.get("default_issue_type") or "").strip()
    types = section.get("issue_type_by_project")
    return bool(default_type) or (isinstance(types, dict) and bool(types))


def _has_nonempty_picker_override(section: dict[str, Any]) -> bool:
    statuses = section.get("statuses")
    assignee = str(section.get("assignee") or "").strip()
    return (isinstance(statuses, list) and bool(statuses)) or bool(assignee)


def _section_is_custom(section: Any, *, kind: str) -> bool:
    if not isinstance(section, dict):
        return False
    mode = _section_mode(section)
    if mode == "custom":
        return True
    if mode == "inherit":
        return False
    if kind == "repo_picker":
        return _has_nonempty_repos(section)
    if kind == "jira_filing":
        return _has_nonempty_filing_override(section)
    if kind == "ticket_picker":
        return _has_nonempty_picker_override(section)
    return False


def _normalize_repos(repos: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(repos, list):
        return normalized
    for item in repos:
        if not isinstance(item, dict):
            continue
        repo_id = str(item.get("id") or "").strip()
        if not repo_id:
            continue
        branch = str(item.get("default_branch") or DEFAULT_BASE_BRANCH).strip()
        normalized.append(
            {"id": repo_id, "default_branch": branch or DEFAULT_BASE_BRANCH}
        )
    return normalized


def resolve_program_runtime_config(
    program_snapshot: dict[str, Any] | None,
    runtime_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = default_program_runtime_config()
    defaults = _as_dict(runtime_defaults)
    snapshot = _as_dict(program_snapshot)
    overlay = _as_dict(snapshot.get("config"))
    for key in CONFIG_SECTIONS:
        base = _as_dict(merged.get(key))
        base.update(_as_dict(defaults.get(key)))
        if _section_is_custom(overlay.get(key), kind=key):
            base.update(_as_dict(overlay.get(key)))
        merged[key] = {k: v for k, v in base.items() if k != "mode"}

    repo_section = overlay.get("repo_picker")
    if _section_is_custom(repo_section, kind="repo_picker"):
        repos = _as_dict(repo_section).get("repos")
    else:
        repos = _as_dict(defaults.get("repo_picker")).get("repos")
    merged["repo_picker"]["repos"] = _normalize_repos(repos)

    picker_section = overlay.get("ticket_picker")
    if _section_is_custom(picker_section, kind="ticket_picker"):
        statuses = _as_dict(picker_section).get("statuses")
    else:
        statuses = _as_dict(defaults.get("ticket_picker")).get("statuses")
    if isinstance(statuses, list) and statuses:
        merged["ticket_picker"]["statuses"] = [str(item) for item in statuses]
    else:
        merged["ticket_picker"]["statuses"] = list(DEFAULT_TICKET_STATUSES)

    filing_section = overlay.get("jira_filing")
    if _section_is_custom(filing_section, kind="jira_filing"):
        types = _as_dict(filing_section).get("issue_type_by_project")
        default_type = _as_dict(filing_section).get("default_issue_type")
    else:
        types = _as_dict(defaults.get("jira_filing")).get("issue_type_by_project")
        default_type = _as_dict(defaults.get("jira_filing")).get("default_issue_type")
    if isinstance(types, dict):
        merged["jira_filing"]["issue_type_by_project"] = {
            str(key): str(value) for key, value in types.items()
        }
    merged["jira_filing"]["default_issue_type"] = (
        str(default_type or DEFAULT_ISSUE_TYPE).strip() or DEFAULT_ISSUE_TYPE
    )
    return deepcopy(merged)


def resolved_program_config(
    snapshot: dict[str, Any] | None,
    runtime_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return resolve_program_runtime_config(snapshot, runtime_defaults)


def allowlist_branch_for_repo(config: dict[str, Any], repo_id: str | None) -> str | None:
    if not repo_id:
        return None
    for item in (config.get("repo_picker") or {}).get("repos") or []:
        if str(item.get("id") or "") == repo_id:
            return str(item.get("default_branch") or "").strip() or None
    return None


def _validate_mode(section: dict[str, Any], *, path: str, errors: list[str]) -> str | None:
    if "mode" not in section:
        return None
    mode = str(section.get("mode") or "").strip().lower()
    if mode not in VALID_MODES:
        errors.append(f"{path}.mode must be inherit or custom")
        return mode or None
    return mode


def validate_program_config(config: Any, *, path: str = "$.config") -> list[str]:
    errors: list[str] = []
    if config is None:
        return errors
    if not isinstance(config, dict):
        return [f"{path} must be an object"]
    known = set(CONFIG_SECTIONS)
    unknown = sorted(str(key) for key in config if key not in known)
    if unknown:
        errors.append(f"{path} contains unknown field(s): {', '.join(unknown)}")
    repo_picker = config.get("repo_picker")
    if repo_picker is not None:
        if not isinstance(repo_picker, dict):
            errors.append(f"{path}.repo_picker must be an object")
        else:
            mode = _validate_mode(repo_picker, path=f"{path}.repo_picker", errors=errors)
            repos = repo_picker.get("repos")
            require_repos = mode == "custom" or (mode is None and "repos" in repo_picker)
            if mode == "custom" and repos is None:
                errors.append(f"{path}.repo_picker.repos is required when mode is custom")
            elif require_repos and repos is None and mode != "inherit":
                errors.append(f"{path}.repo_picker.repos is required when repo_picker is set")
            elif repos is not None and not isinstance(repos, list):
                errors.append(f"{path}.repo_picker.repos must be an array")
            elif isinstance(repos, list) and mode != "inherit":
                for index, item in enumerate(repos):
                    if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                        errors.append(f"{path}.repo_picker.repos[{index}].id is required")
    filing = config.get("jira_filing")
    if filing is not None:
        if not isinstance(filing, dict):
            errors.append(f"{path}.jira_filing must be an object")
        else:
            mode = _validate_mode(filing, path=f"{path}.jira_filing", errors=errors)
            if mode != "inherit" and not str(filing.get("default_issue_type") or "").strip():
                errors.append(f"{path}.jira_filing.default_issue_type is required")
    picker = config.get("ticket_picker")
    if picker is not None:
        if not isinstance(picker, dict):
            errors.append(f"{path}.ticket_picker must be an object")
        else:
            _validate_mode(picker, path=f"{path}.ticket_picker", errors=errors)
    return errors
