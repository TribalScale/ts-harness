"""Validate and merge managed runtime-settings overlays."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

import yaml

from ts_harness.jira.pointer_block import DEFAULT_BASE_BRANCH
from ts_harness.policy.permissions import DEPARTMENT_NAME_RE, PERMISSIONS

ALLOWED_TOP = (
    "definition_type",
    "definition_version",
    "program_defaults",
    "identity",
    "policy",
    "sandbox",
    "bindings",
    "toolsets",
    "connectors",
)
ALLOWED_POLICY = (
    "approval_required_tools",
    "channel_ingress_deny",
    "delegation_auto_allow_agents",
    "budget_warn_usd",
    "budget_hard_stop_usd",
    "model_allowlist",
    "data_residency_regions",
    "egress_sensitive_tools",
    "network_egress_deny_tools",
    "rate_limit_per_minute",
    "opa_enabled",
)
ALLOWED_SANDBOX = ("mode", "backend", "cpu_limit", "memory_mb", "timeout_seconds", "egress_policy")
ALLOWED_CONNECTOR = ("enabled", "auth", "accounts", "scopes", "artifact_publish")
CHANNEL_INGRESS_TOKENS = frozenset({"exec", "write_file", "write", "delegation", "workflow_update"})
SECRET_NAME_FIELDS = frozenset(
    {"credentials_secret", "client_id_secret", "client_secret_secret"}
)
ALLOWED_DEFAULTS = (
    "repo_picker",
    "jira_filing",
    "ticket_picker",
    "github",
    "governance",
    "merge_readiness",
    "release",
    "delivery",
    "compensation",
)
ALLOWED_DELIVERY = ("max_parallel_work_orders",)
ALLOWED_COMPENSATION = ("jira", "jira_close_transition", "delete_branches")
COMPENSATION_JIRA_MODES = ("label", "close")
MAX_PARALLEL_WORK_ORDERS_LIMIT = 10
ALLOWED_RELEASE = ("environments",)
ALLOWED_ENVIRONMENT = ("name", "trigger", "required_roles", "required_permission", "requires")
ALLOWED_TRIGGER = {"workflow_dispatch": ("kind", "workflow", "ref_input"), "deployment": ("kind",)}
ENVIRONMENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
ALLOWED_GOVERNANCE = ("independent_review",)
ALLOWED_MERGE_READINESS = ("required_checks", "policy")
ALLOWED_MERGE_POLICY = (
    "mode",
    "required_categories",
    "categories",
    "missing_check_grace_seconds",
    "require_branch_protection",
    "diff_limits",
    "host_security",
)
MERGE_POLICY_MODES = ("warn", "enforce")
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED_INDEPENDENT_REVIEW = ("enabled",)
ALLOWED_IDENTITY = (
    "slack_user_emails",
    "jira_account_ids",
    "github_logins",
    "roles",
    "role_bundles",
    "departments",
    "service_permissions",
    "group_roles",
)
ALLOWED_REPO_PICKER = ("repos", "mode")
ALLOWED_FILING = ("default_issue_type", "issue_type_by_project", "allowed_projects", "mode")
ALLOWED_GITHUB = ("org",)
JIRA_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
ALLOWED_PICKER = ("assignee", "statuses", "mode")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEFINITION_TYPE = "runtime_settings"


def _issue(code: str, message: str, *, path: str = "$") -> dict[str, str]:
    return {"severity": "error", "code": code, "path": path, "message": message}


def _is_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value))


def _unknown_keys(payload: dict[str, Any], allowed: tuple[str, ...], path: str) -> list[dict[str, str]]:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if not unknown:
        return []
    return [
        _issue(
            "unknown_field",
            f"{path} contains unknown field(s): {', '.join(unknown)}",
            path=path,
        )
    ]


def _canonicalize_repos(repos: Any, *, path: str, issues: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(repos, list):
        issues.append(_issue("invalid_repos", f"{path} must be an array", path=path))
        return []
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for index, item in enumerate(repos):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue("invalid_repo", f"{item_path} must be an object", path=item_path))
            continue
        repo_id = str(item.get("id") or "").strip()
        if not repo_id:
            continue
        if repo_id in seen:
            issues.append(
                _issue("duplicate_repo_id", f"duplicate repo id: {repo_id}", path=item_path)
            )
            continue
        seen.add(repo_id)
        branch = str(item.get("default_branch") or DEFAULT_BASE_BRANCH).strip()
        out.append({"id": repo_id, "default_branch": branch or DEFAULT_BASE_BRANCH})
    return out


def _canonicalize_email_map(
    raw: Any,
    *,
    path: str,
    issues: list[dict[str, str]],
    keys_are_emails: bool,
) -> dict[str, str]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object", path=path))
        return {}
    out: dict[str, str] = {}
    seen: set[str] = set()
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip()
        value = str(raw_value or "").strip()
        email = key.lower() if keys_are_emails else key
        if keys_are_emails:
            if not _is_email(email):
                issues.append(_issue("invalid_email", f"{path} key is not an email: {key}", path=path))
                continue
            if email in seen:
                issues.append(_issue("duplicate_email", f"duplicate email: {email}", path=path))
                continue
            seen.add(email)
            if value:
                out[email] = value
        else:
            mapped = value.lower()
            if mapped and not _is_email(mapped):
                issues.append(_issue("invalid_email", f"{path}.{key} is not an email", path=path))
                continue
            if mapped in seen:
                issues.append(_issue("duplicate_email", f"duplicate email: {mapped}", path=path))
                continue
            if mapped:
                seen.add(mapped)
                out[key] = mapped
    return out


def _canonicalize_jira_projects(raw: Any, *, path: str, issues: list[dict[str, str]]) -> list[str]:
    if not isinstance(raw, list):
        issues.append(_issue("invalid_projects", f"{path} must be an array", path=path))
        return []
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip().upper()
        if not key:
            continue
        if not JIRA_PROJECT_RE.match(key):
            issues.append(_issue("invalid_jira_project", f"invalid Jira project key: {key}", path=path))
            continue
        if key not in out:
            out.append(key)
    return out


def _canonicalize_github_logins(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, str]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object", path=path))
        return {}
    out: dict[str, str] = {}
    owners: dict[str, str] = {}
    for raw_email, raw_login in raw.items():
        email = str(raw_email or "").strip().lower()
        login = str(raw_login or "").strip()
        if not _is_email(email):
            issues.append(_issue("invalid_email", f"{path} key is not an email: {raw_email}", path=path))
            continue
        if not login:
            continue
        if not GITHUB_LOGIN_RE.match(login):
            issues.append(_issue("invalid_github_login", f"invalid GitHub login: {login}", path=path))
            continue
        folded = login.lower()
        if folded in owners and owners[folded] != email:
            issues.append(
                _issue("duplicate_github_login", f"GitHub login mapped twice: {login}", path=path)
            )
            continue
        owners[folded] = email
        out[email] = login
    return out


def _canonicalize_roles(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object", path=path))
        return {}
    out: dict[str, list[str]] = {}
    for raw_email, raw_roles in raw.items():
        email = str(raw_email or "").strip().lower()
        if not _is_email(email):
            issues.append(_issue("invalid_email", f"{path} key is not an email: {raw_email}", path=path))
            continue
        if not isinstance(raw_roles, list):
            issues.append(_issue("invalid_roles", f"{path}.{email} must be an array", path=path))
            continue
        roles = sorted({str(role).strip() for role in raw_roles if str(role or "").strip()})
        out[email] = roles
    return out


def _canonicalize_governance(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_GOVERNANCE, path))
    out: dict[str, Any] = {}
    if "independent_review" in raw:
        review = raw.get("independent_review")
        review_path = f"{path}.independent_review"
        if not isinstance(review, dict):
            issues.append(_issue("invalid_section", f"{review_path} must be an object", path=review_path))
        else:
            issues.extend(_unknown_keys(review, ALLOWED_INDEPENDENT_REVIEW, review_path))
            enabled = review.get("enabled", True)
            if not isinstance(enabled, bool):
                issues.append(
                    _issue(
                        "invalid_boolean",
                        f"{review_path}.enabled must be true or false",
                        path=f"{review_path}.enabled",
                    )
                )
                enabled = True
            out["independent_review"] = {"enabled": enabled}
    return out


def _canonicalize_repo_name_lists(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object keyed by owner/repo", path=path))
        return {}
    out: dict[str, list[str]] = {}
    for raw_repo, raw_names in raw.items():
        repo = str(raw_repo or "").strip()
        if not REPO_SLUG_RE.match(repo):
            issues.append(_issue("invalid_repo_slug", f"{path} key is not owner/repo: {raw_repo}", path=path))
            continue
        if not isinstance(raw_names, list):
            issues.append(_issue("invalid_names", f"{path}.{repo} must be an array", path=path))
            continue
        names: list[str] = []
        for name in raw_names:
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
        out[repo.lower()] = names
    return out


def _canonicalize_environment(
    raw: Any, *, path: str, issues: list[dict[str, str]], known: list[str]
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_environment", f"{path} must be an object", path=path))
        return None
    issues.extend(_unknown_keys(raw, ALLOWED_ENVIRONMENT, path))
    name = str(raw.get("name") or "").strip().lower()
    if not ENVIRONMENT_NAME_RE.match(name):
        issues.append(_issue("invalid_environment_name", f"{path}.name is not a valid environment name", path=path))
        return None
    if name in known:
        issues.append(_issue("duplicate_environment", f"duplicate environment: {name}", path=path))
        return None
    trigger_raw = raw.get("trigger") or {"kind": "deployment"}
    if not isinstance(trigger_raw, dict) or trigger_raw.get("kind") not in ALLOWED_TRIGGER:
        issues.append(
            _issue(
                "invalid_trigger",
                f"{path}.trigger.kind must be one of: {', '.join(sorted(ALLOWED_TRIGGER))}",
                path=f"{path}.trigger",
            )
        )
        return None
    kind = str(trigger_raw["kind"])
    issues.extend(_unknown_keys(trigger_raw, ALLOWED_TRIGGER[kind], f"{path}.trigger"))
    trigger: dict[str, Any] = {"kind": kind}
    if kind == "workflow_dispatch":
        workflow = str(trigger_raw.get("workflow") or "").strip()
        if not workflow:
            issues.append(_issue("invalid_trigger", f"{path}.trigger.workflow is required", path=f"{path}.trigger"))
            return None
        trigger["workflow"] = workflow
        trigger["ref_input"] = str(trigger_raw.get("ref_input") or "sha").strip() or "sha"
    roles_raw = raw.get("required_roles") or []
    if not isinstance(roles_raw, list):
        issues.append(_issue("invalid_roles", f"{path}.required_roles must be an array", path=path))
        roles_raw = []
    permission = str(raw.get("required_permission") or "").strip().lower()
    if permission and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", permission):
        issues.append(
            _issue(
                "invalid_permission",
                f"{path}.required_permission must be a permission name",
                path=path,
            )
        )
        return None
    requires = str(raw.get("requires") or "").strip().lower() or None
    if requires and requires not in known:
        issues.append(
            _issue("invalid_requires", f"{path}.requires must name an earlier environment: {requires}", path=path)
        )
        return None
    return {
        "name": name,
        "trigger": trigger,
        "required_roles": sorted({str(role).strip().lower() for role in roles_raw if str(role or "").strip()}),
        "required_permission": permission,
        "requires": requires,
    }


def _canonicalize_release(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_RELEASE, path))
    envs_raw = raw.get("environments") or {}
    env_path = f"{path}.environments"
    if not isinstance(envs_raw, dict):
        issues.append(_issue("invalid_map", f"{env_path} must be an object keyed by owner/repo", path=env_path))
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for raw_repo, raw_list in envs_raw.items():
        repo = str(raw_repo or "").strip()
        repo_path = f"{env_path}.{repo}"
        if not REPO_SLUG_RE.match(repo):
            issues.append(_issue("invalid_repo_slug", f"{env_path} key is not owner/repo: {raw_repo}", path=env_path))
            continue
        if not isinstance(raw_list, list):
            issues.append(_issue("invalid_environments", f"{repo_path} must be an array", path=repo_path))
            continue
        envs: list[dict[str, Any]] = []
        for index, item in enumerate(raw_list):
            env = _canonicalize_environment(
                item, path=f"{repo_path}[{index}]", issues=issues, known=[e["name"] for e in envs]
            )
            if env:
                envs.append(env)
        if len(raw_list) != 2:
            issues.append(
                _issue(
                    "invalid_environments",
                    f"{repo_path} must list two environments: pre-production, then production",
                    path=repo_path,
                )
            )
            continue
        out[repo.lower()] = envs
    return {"environments": out}


def _canonicalize_delivery(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_DELIVERY, path))
    out: dict[str, Any] = {}
    if "max_parallel_work_orders" in raw:
        value = raw.get("max_parallel_work_orders")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PARALLEL_WORK_ORDERS_LIMIT:
            issues.append(
                _issue(
                    "invalid_max_parallel",
                    f"{path}.max_parallel_work_orders must be an integer from 1 to {MAX_PARALLEL_WORK_ORDERS_LIMIT}",
                    path=f"{path}.max_parallel_work_orders",
                )
            )
        else:
            out["max_parallel_work_orders"] = value
    return out


def _canonicalize_compensation(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_COMPENSATION, path))
    out: dict[str, Any] = {}
    if "jira" in raw:
        mode = str(raw.get("jira") or "").strip().lower()
        if mode not in COMPENSATION_JIRA_MODES:
            issues.append(
                _issue(
                    "invalid_compensation_jira",
                    f"{path}.jira must be one of: {', '.join(COMPENSATION_JIRA_MODES)}",
                    path=f"{path}.jira",
                )
            )
        else:
            out["jira"] = mode
    if "jira_close_transition" in raw:
        transition = str(raw.get("jira_close_transition") or "").strip()
        if not transition:
            issues.append(
                _issue(
                    "invalid_compensation_transition",
                    f"{path}.jira_close_transition must be a non-empty transition name",
                    path=f"{path}.jira_close_transition",
                )
            )
        else:
            out["jira_close_transition"] = transition
    if "delete_branches" in raw:
        value = raw.get("delete_branches")
        if not isinstance(value, bool):
            issues.append(
                _issue(
                    "invalid_compensation_branches",
                    f"{path}.delete_branches must be true or false",
                    path=f"{path}.delete_branches",
                )
            )
        else:
            out["delete_branches"] = value
    return out


def _canonicalize_merge_readiness(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_MERGE_READINESS, path))
    out: dict[str, Any] = {}
    if "required_checks" in raw:
        out["required_checks"] = _canonicalize_repo_name_lists(
            raw.get("required_checks"), path=f"{path}.required_checks", issues=issues
        )
    if "policy" in raw:
        out["policy"] = _canonicalize_merge_policy(raw.get("policy"), path=f"{path}.policy", issues=issues)
    return out


def _canonicalize_merge_policy(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_MERGE_POLICY, path))
    out: dict[str, Any] = {}
    if "mode" in raw:
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in MERGE_POLICY_MODES:
            issues.append(_issue("invalid_policy_mode", f"{path}.mode must be warn or enforce", path=f"{path}.mode"))
        else:
            out["mode"] = mode
    if "required_categories" in raw:
        value = raw.get("required_categories")
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            issues.append(
                _issue(
                    "invalid_required_categories",
                    f"{path}.required_categories must be an array of category names",
                    path=f"{path}.required_categories",
                )
            )
        else:
            out["required_categories"] = list(dict.fromkeys(item.strip() for item in value))
    if "categories" in raw:
        out["categories"] = _canonicalize_policy_categories(
            raw.get("categories"), path=f"{path}.categories", issues=issues
        )
    if "missing_check_grace_seconds" in raw:
        value = raw.get("missing_check_grace_seconds")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            issues.append(
                _issue(
                    "invalid_grace_seconds",
                    f"{path}.missing_check_grace_seconds must be a non-negative integer",
                    path=f"{path}.missing_check_grace_seconds",
                )
            )
        else:
            out["missing_check_grace_seconds"] = value
    if "require_branch_protection" in raw:
        value = raw.get("require_branch_protection")
        if not isinstance(value, bool):
            issues.append(
                _issue(
                    "invalid_branch_protection",
                    f"{path}.require_branch_protection must be true or false",
                    path=f"{path}.require_branch_protection",
                )
            )
        else:
            out["require_branch_protection"] = value
    if "diff_limits" in raw:
        out["diff_limits"] = _canonicalize_diff_limits(raw.get("diff_limits"), path=f"{path}.diff_limits", issues=issues)
    if "host_security" in raw:
        out["host_security"] = _canonicalize_host_security(
            raw.get("host_security"),
            path=f"{path}.host_security",
            issues=issues,
            enforce=out.get("mode") == "enforce",
        )
    return out


ALLOWED_HOST_SECURITY = ("secret_scan", "dependency_audit", "audit_severity", "static_analysis")
AUDIT_SEVERITIES = ("low", "moderate", "high", "critical")


def _canonicalize_host_security(
    raw: Any, *, path: str, issues: list[dict[str, str]], enforce: bool
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_HOST_SECURITY, path))
    out: dict[str, Any] = {}
    for key in ("secret_scan", "dependency_audit", "static_analysis"):
        if key not in raw:
            continue
        if not isinstance(raw.get(key), bool):
            issues.append(_issue("invalid_host_security", f"{path}.{key} must be true or false", path=f"{path}.{key}"))
        else:
            out[key] = raw[key]
    if "audit_severity" in raw:
        severity = str(raw.get("audit_severity") or "").strip().lower()
        if severity not in AUDIT_SEVERITIES:
            issues.append(
                _issue(
                    "invalid_host_security",
                    f"{path}.audit_severity must be one of {', '.join(AUDIT_SEVERITIES)}",
                    path=f"{path}.audit_severity",
                )
            )
        else:
            out["audit_severity"] = severity
    if enforce and out.get("secret_scan") is False:
        issues.append(
            _issue(
                "secret_scan_required",
                f"{path}.secret_scan cannot be disabled when the merge policy mode is enforce",
                path=f"{path}.secret_scan",
            )
        )
    return out


ALLOWED_DIFF_LIMITS = ("max_files", "max_changed_lines", "exclude")


def _canonicalize_diff_limits(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_DIFF_LIMITS, path))
    out: dict[str, Any] = {}
    for key in ("max_files", "max_changed_lines"):
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            issues.append(_issue("invalid_diff_limit", f"{path}.{key} must be a positive integer", path=f"{path}.{key}"))
        else:
            out[key] = value
    if "exclude" in raw:
        value = raw.get("exclude")
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            issues.append(_issue("invalid_diff_limit", f"{path}.exclude must be an array of glob strings", path=f"{path}.exclude"))
        else:
            out["exclude"] = list(dict.fromkeys(item.strip() for item in value))
    return out


def _canonicalize_policy_categories(
    raw: Any, *, path: str, issues: list[dict[str, str]]
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object keyed by owner/repo", path=path))
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for raw_repo, raw_categories in raw.items():
        repo = str(raw_repo or "").strip()
        if not REPO_SLUG_RE.match(repo):
            issues.append(_issue("invalid_repo_slug", f"{path} key is not owner/repo: {raw_repo}", path=path))
            continue
        if not isinstance(raw_categories, dict):
            issues.append(_issue("invalid_map", f"{path}.{repo} must be an object keyed by category", path=path))
            continue
        categories: dict[str, list[str]] = {}
        for raw_name, raw_names in raw_categories.items():
            name = str(raw_name or "").strip()
            if not name or not isinstance(raw_names, list):
                issues.append(
                    _issue("invalid_names", f"{path}.{repo}.{raw_name} must be an array of check names", path=path)
                )
                continue
            categories[name] = list(dict.fromkeys(str(item).strip() for item in raw_names if str(item or "").strip()))
        out[repo.lower()] = categories
    return out


def _canonicalize_program_defaults(
    raw: Any, *, path: str, issues: list[dict[str, str]]
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_DEFAULTS, path))
    out: dict[str, Any] = {}
    if "repo_picker" in raw:
        picker = raw.get("repo_picker")
        if not isinstance(picker, dict):
            issues.append(_issue("invalid_section", f"{path}.repo_picker must be an object", path=f"{path}.repo_picker"))
        else:
            issues.extend(_unknown_keys(picker, ALLOWED_REPO_PICKER, f"{path}.repo_picker"))
            repos = _canonicalize_repos(
                picker.get("repos") if "repos" in picker else [],
                path=f"{path}.repo_picker.repos",
                issues=issues,
            )
            picker_out: dict[str, Any] = {"repos": repos}
            mode = picker.get("mode")
            if isinstance(mode, str) and mode.strip():
                picker_out["mode"] = mode.strip()
            out["repo_picker"] = picker_out
    if "jira_filing" in raw:
        filing = raw.get("jira_filing")
        if not isinstance(filing, dict):
            issues.append(_issue("invalid_section", f"{path}.jira_filing must be an object", path=f"{path}.jira_filing"))
        else:
            issues.extend(_unknown_keys(filing, ALLOWED_FILING, f"{path}.jira_filing"))
            types = filing.get("issue_type_by_project") or {}
            if types and not isinstance(types, dict):
                issues.append(
                    _issue(
                        "invalid_map",
                        f"{path}.jira_filing.issue_type_by_project must be an object",
                        path=f"{path}.jira_filing.issue_type_by_project",
                    )
                )
                types = {}
            out["jira_filing"] = {
                "default_issue_type": str(filing.get("default_issue_type") or "Story").strip()
                or "Story",
                "issue_type_by_project": {
                    str(key): str(value) for key, value in dict(types).items()
                },
            }
            if "allowed_projects" in filing:
                out["jira_filing"]["allowed_projects"] = _canonicalize_jira_projects(
                    filing.get("allowed_projects"),
                    path=f"{path}.jira_filing.allowed_projects",
                    issues=issues,
                )
    if "github" in raw:
        github = raw.get("github")
        if not isinstance(github, dict):
            issues.append(_issue("invalid_section", f"{path}.github must be an object", path=f"{path}.github"))
        else:
            issues.extend(_unknown_keys(github, ALLOWED_GITHUB, f"{path}.github"))
            org = str(github.get("org") or "").strip()
            if org and not GITHUB_LOGIN_RE.match(org):
                issues.append(_issue("invalid_github_org", f"invalid GitHub org: {org}", path=f"{path}.github.org"))
            out["github"] = {"org": org}
    if "governance" in raw:
        out["governance"] = _canonicalize_governance(
            raw.get("governance"), path=f"{path}.governance", issues=issues
        )
    if "merge_readiness" in raw:
        out["merge_readiness"] = _canonicalize_merge_readiness(
            raw.get("merge_readiness"), path=f"{path}.merge_readiness", issues=issues
        )
    if "release" in raw:
        out["release"] = _canonicalize_release(raw.get("release"), path=f"{path}.release", issues=issues)
    if "delivery" in raw:
        out["delivery"] = _canonicalize_delivery(raw.get("delivery"), path=f"{path}.delivery", issues=issues)
    if "compensation" in raw:
        out["compensation"] = _canonicalize_compensation(
            raw.get("compensation"), path=f"{path}.compensation", issues=issues
        )
    if "ticket_picker" in raw:
        picker = raw.get("ticket_picker")
        if not isinstance(picker, dict):
            issues.append(
                _issue("invalid_section", f"{path}.ticket_picker must be an object", path=f"{path}.ticket_picker")
            )
        else:
            issues.extend(_unknown_keys(picker, ALLOWED_PICKER, f"{path}.ticket_picker"))
            statuses = picker.get("statuses") or []
            if not isinstance(statuses, list):
                issues.append(
                    _issue(
                        "invalid_statuses",
                        f"{path}.ticket_picker.statuses must be an array",
                        path=f"{path}.ticket_picker.statuses",
                    )
                )
                statuses = []
            out["ticket_picker"] = {
                "assignee": "mapped_user",
                "statuses": [str(item) for item in statuses],
            }
    return out


def _canonicalize_identity(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_section", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_IDENTITY, path))
    out: dict[str, Any] = {}
    if "slack_user_emails" in raw:
        out["slack_user_emails"] = _canonicalize_email_map(
            raw.get("slack_user_emails"),
            path=f"{path}.slack_user_emails",
            issues=issues,
            keys_are_emails=False,
        )
    if "jira_account_ids" in raw:
        out["jira_account_ids"] = _canonicalize_email_map(
            raw.get("jira_account_ids"),
            path=f"{path}.jira_account_ids",
            issues=issues,
            keys_are_emails=True,
        )
    if "github_logins" in raw:
        out["github_logins"] = _canonicalize_github_logins(
            raw.get("github_logins"), path=f"{path}.github_logins", issues=issues
        )
    if "roles" in raw:
        out["roles"] = _canonicalize_roles(raw.get("roles"), path=f"{path}.roles", issues=issues)
    if "departments" in raw:
        out["departments"] = _canonicalize_departments(
            raw.get("departments"), path=f"{path}.departments", issues=issues
        )
    if "role_bundles" in raw:
        out["role_bundles"] = _canonicalize_role_bundles(
            raw.get("role_bundles"), path=f"{path}.role_bundles", issues=issues
        )
    if "service_permissions" in raw:
        out["service_permissions"] = _canonicalize_permission_list(
            raw.get("service_permissions"), path=f"{path}.service_permissions", issues=issues
        )
    if "group_roles" in raw:
        out["group_roles"] = _canonicalize_group_roles(
            raw.get("group_roles"), path=f"{path}.group_roles", issues=issues
        )
    return out


def _canonicalize_permission_list(raw: Any, *, path: str, issues: list[dict[str, str]]) -> list[str]:
    if not isinstance(raw, list):
        issues.append(_issue("invalid_permissions", f"{path} must be an array", path=path))
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip().lower()
        if not name:
            continue
        if name not in PERMISSIONS:
            issues.append(_issue("unknown_permission", f"{path} has unknown permission {name}", path=path))
            continue
        if name not in out:
            out.append(name)
    return out


def _canonicalize_departments(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, str]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object", path=path))
        return {}
    out: dict[str, str] = {}
    for raw_email, raw_name in raw.items():
        email = str(raw_email or "").strip().lower()
        if not _is_email(email):
            issues.append(_issue("invalid_email", f"{path} key is not an email: {raw_email}", path=path))
            continue
        name = str(raw_name or "").strip().lower()
        if not DEPARTMENT_NAME_RE.fullmatch(name):
            issues.append(
                _issue("invalid_department", f"{path}.{email} is not a department name", path=path)
            )
            continue
        out[email] = name
    return out


def _canonicalize_role_bundles(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object", path=path))
        return {}
    out: dict[str, list[str]] = {}
    for raw_role, raw_perms in raw.items():
        role = str(raw_role or "").strip().lower()
        if not role:
            continue
        out[role] = _canonicalize_permission_list(raw_perms, path=f"{path}.{role}", issues=issues)
    return out


def _canonicalize_group_roles(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object", path=path))
        return {}
    out: dict[str, list[str]] = {}
    for raw_group, raw_roles in raw.items():
        group = str(raw_group or "").strip()
        if not group:
            continue
        if not isinstance(raw_roles, list):
            issues.append(_issue("invalid_roles", f"{path}.{group} must be an array", path=path))
            continue
        roles = sorted({str(role).strip().lower() for role in raw_roles if str(role or "").strip()})
        out[group] = roles
    return out


def _looks_like_secret_material(value: str) -> bool:
    text = value.strip()
    if "BEGIN " in text and "PRIVATE KEY" in text:
        return True
    if text.startswith("{") and text.endswith("}"):
        return True
    if len(text) > 80 and " " not in text and text.count(".") < 2:
        return True
    return False


def _string_list(
    raw: Any,
    *,
    path: str,
    issues: list[dict[str, str]],
    known: set[str] | None = None,
) -> list[str]:
    if not isinstance(raw, list):
        issues.append(_issue("invalid_list", f"{path} must be an array", path=path))
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if known is not None and name not in known:
            issues.append(_issue("unknown_name", f"unknown name: {name}", path=path))
            continue
        out.append(name)
    return out


def _canonicalize_policy(
    raw: Any,
    *,
    path: str,
    issues: list[dict[str, str]],
    known_agent_ids: set[str] | None,
    known_tool_names: set[str] | None,
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_policy", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_POLICY, path))
    out: dict[str, Any] = {}
    tool_known = None if known_tool_names is None else set(known_tool_names)
    ingress_known = None if tool_known is None else set(tool_known) | set(CHANNEL_INGRESS_TOKENS)
    for key in ("approval_required_tools", "egress_sensitive_tools", "network_egress_deny_tools"):
        if key in raw:
            out[key] = _string_list(raw[key], path=f"{path}.{key}", issues=issues, known=tool_known)
    if "channel_ingress_deny" in raw:
        out["channel_ingress_deny"] = _string_list(
            raw["channel_ingress_deny"],
            path=f"{path}.channel_ingress_deny",
            issues=issues,
            known=ingress_known,
        )
    if "delegation_auto_allow_agents" in raw:
        agents = _string_list(
            raw["delegation_auto_allow_agents"],
            path=f"{path}.delegation_auto_allow_agents",
            issues=issues,
            known=known_agent_ids,
        )
        out["delegation_auto_allow_agents"] = agents
    warn = raw.get("budget_warn_usd", None)
    hard = raw.get("budget_hard_stop_usd", None)
    if "budget_warn_usd" in raw or "budget_hard_stop_usd" in raw:
        try:
            warn_n = float(warn if warn is not None else 0)
            hard_n = float(hard if hard is not None else warn_n)
        except (TypeError, ValueError):
            issues.append(_issue("invalid_budget", f"{path} budgets must be numbers", path=path))
            warn_n = 0.0
            hard_n = 0.0
        if warn_n < 0 or hard_n < 0 or warn_n > hard_n:
            issues.append(
                _issue(
                    "invalid_budget",
                    f"{path} warn must be <= hard stop and both must be >= 0",
                    path=path,
                )
            )
        if "budget_warn_usd" in raw:
            out["budget_warn_usd"] = warn_n
        if "budget_hard_stop_usd" in raw:
            out["budget_hard_stop_usd"] = hard_n
    if "rate_limit_per_minute" in raw:
        try:
            limit = int(raw["rate_limit_per_minute"])
        except (TypeError, ValueError):
            limit = -1
        if limit < 0:
            issues.append(_issue("invalid_rate_limit", f"{path}.rate_limit_per_minute must be >= 0", path=path))
        else:
            out["rate_limit_per_minute"] = limit
    if "opa_enabled" in raw:
        if not isinstance(raw["opa_enabled"], bool):
            issues.append(_issue("invalid_bool", f"{path}.opa_enabled must be a boolean", path=path))
        else:
            out["opa_enabled"] = raw["opa_enabled"]
    if "data_residency_regions" in raw:
        regions = raw["data_residency_regions"]
        if not isinstance(regions, dict):
            issues.append(_issue("invalid_map", f"{path}.data_residency_regions must be an object", path=path))
        else:
            out["data_residency_regions"] = deepcopy(regions)
    if "model_allowlist" in raw:
        allowlist = raw["model_allowlist"]
        if not isinstance(allowlist, dict):
            issues.append(_issue("invalid_map", f"{path}.model_allowlist must be an object", path=path))
        else:
            cleaned: dict[str, list[str]] = {}
            for agent_id, models in allowlist.items():
                if known_agent_ids is not None and str(agent_id) not in known_agent_ids:
                    issues.append(
                        _issue("unknown_agent", f"unknown agent: {agent_id}", path=f"{path}.model_allowlist")
                    )
                    continue
                cleaned[str(agent_id)] = _string_list(
                    models, path=f"{path}.model_allowlist.{agent_id}", issues=issues
                )
            out["model_allowlist"] = cleaned
    return out


def _canonicalize_sandbox(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_sandbox", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_SANDBOX, path))
    out: dict[str, Any] = {}
    for key in ("mode", "backend"):
        if key in raw and str(raw[key]) != "docker":
            issues.append(_issue("invalid_sandbox", f"{path}.{key} must be docker", path=f"{path}.{key}"))
        elif key in raw:
            out[key] = "docker"
    for key in ("cpu_limit", "memory_mb", "timeout_seconds"):
        if key not in raw:
            continue
        try:
            number = float(raw[key])
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            issues.append(_issue("invalid_sandbox", f"{path}.{key} must be > 0", path=f"{path}.{key}"))
        else:
            out[key] = int(number) if key != "cpu_limit" else number
    if "egress_policy" in raw:
        policy = str(raw["egress_policy"])
        if policy not in {"deny", "allow"}:
            issues.append(
                _issue("invalid_sandbox", f"{path}.egress_policy must be deny or allow", path=f"{path}.egress_policy")
            )
        else:
            out["egress_policy"] = policy
    return out


def _canonicalize_bindings(
    raw: Any,
    *,
    path: str,
    issues: list[dict[str, str]],
    known_agent_ids: set[str] | None,
) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_bindings", f"{path} must be an object", path=path))
        return {}
    out: dict[str, str] = {}
    for channel, agent_id in raw.items():
        agent = str(agent_id or "").strip()
        if known_agent_ids is not None and agent not in known_agent_ids:
            issues.append(_issue("unknown_agent", f"unknown agent: {agent}", path=f"{path}.{channel}"))
            continue
        out[str(channel)] = agent
    return out


def _canonicalize_toolsets(
    raw: Any,
    *,
    path: str,
    issues: list[dict[str, str]],
    known_tool_names: set[str] | None,
) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_toolsets", f"{path} must be an object", path=path))
        return {}
    out: dict[str, list[str]] = {}
    for name, tools in raw.items():
        out[str(name)] = _string_list(
            tools,
            path=f"{path}.{name}",
            issues=issues,
            known=known_tool_names,
        )
    return out


def _reject_secret_material(value: Any, *, path: str, issues: list[dict[str, str]]) -> None:
    if isinstance(value, str) and _looks_like_secret_material(value):
        issues.append(_issue("secret_material", f"{path} must be a secret name, not key material", path=path))


def _walk_secret_names(raw: Any, *, path: str, issues: list[dict[str, str]]) -> None:
    if isinstance(raw, dict):
        for key, value in raw.items():
            child = f"{path}.{key}"
            if key in SECRET_NAME_FIELDS:
                _reject_secret_material(value, path=child, issues=issues)
            else:
                _walk_secret_names(value, path=child, issues=issues)


def _canonicalize_connector(
    raw: Any,
    *,
    path: str,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_connector", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ("google-workspace",), path))
    google = raw.get("google-workspace")
    if google is None:
        return {}
    if not isinstance(google, dict):
        issues.append(_issue("invalid_connector", f"{path}.google-workspace must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(google, ALLOWED_CONNECTOR, f"{path}.google-workspace"))
    _walk_secret_names(google, path=f"{path}.google-workspace", issues=issues)
    return {"google-workspace": deepcopy(google)}


def validate_runtime_settings_payload(
    payload: dict[str, Any] | None,
    *,
    known_agent_ids: set[str] | None = None,
    known_tool_names: set[str] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return None, [_issue("invalid_payload", "runtime settings must be an object")]
    issues.extend(_unknown_keys(payload, ALLOWED_TOP, "$"))
    if str(payload.get("definition_type") or "") != DEFINITION_TYPE:
        issues.append(
            _issue(
                "invalid_definition_type",
                "definition_type must be runtime_settings",
                path="$.definition_type",
            )
        )
    version = str(payload.get("definition_version") or "").strip()
    if not version:
        issues.append(_issue("missing_version", "definition_version is required", path="$.definition_version"))
    defaults = _canonicalize_program_defaults(
        payload.get("program_defaults") if "program_defaults" in payload else None,
        path="$.program_defaults",
        issues=issues,
    )
    identity = _canonicalize_identity(
        payload.get("identity") if "identity" in payload else None,
        path="$.identity",
        issues=issues,
    )
    policy = _canonicalize_policy(
        payload.get("policy") if "policy" in payload else None,
        path="$.policy",
        issues=issues,
        known_agent_ids=known_agent_ids,
        known_tool_names=known_tool_names,
    )
    sandbox = _canonicalize_sandbox(
        payload.get("sandbox") if "sandbox" in payload else None,
        path="$.sandbox",
        issues=issues,
    )
    bindings = _canonicalize_bindings(
        payload.get("bindings") if "bindings" in payload else None,
        path="$.bindings",
        issues=issues,
        known_agent_ids=known_agent_ids,
    )
    toolsets = _canonicalize_toolsets(
        payload.get("toolsets") if "toolsets" in payload else None,
        path="$.toolsets",
        issues=issues,
        known_tool_names=known_tool_names,
    )
    connectors = _canonicalize_connector(
        payload.get("connectors") if "connectors" in payload else None,
        path="$.connectors",
        issues=issues,
    )
    if any(issue.get("severity") == "error" for issue in issues):
        return None, issues
    canonical: dict[str, Any] = {
        "definition_type": DEFINITION_TYPE,
        "definition_version": version,
    }
    if "program_defaults" in payload:
        canonical["program_defaults"] = defaults
    if "identity" in payload:
        canonical["identity"] = identity
    if "policy" in payload:
        canonical["policy"] = policy
    if "sandbox" in payload:
        canonical["sandbox"] = sandbox
    if "bindings" in payload:
        canonical["bindings"] = bindings
    if "toolsets" in payload:
        canonical["toolsets"] = toolsets
    if "connectors" in payload:
        canonical["connectors"] = connectors
    return canonical, issues


def _deep_copy_sections(yaml_sections: dict[str, Any]) -> dict[str, Any]:
    return {
        "program_defaults": deepcopy(dict(yaml_sections.get("program_defaults") or {})),
        "identity": deepcopy(dict(yaml_sections.get("identity") or {})),
        "policy": deepcopy(dict(yaml_sections.get("policy") or {})),
        "sandbox": deepcopy(dict(yaml_sections.get("sandbox") or {})),
        "bindings": deepcopy(dict(yaml_sections.get("bindings") or {})),
        "toolsets": deepcopy(dict(yaml_sections.get("toolsets") or {})),
        "connectors": deepcopy(dict(yaml_sections.get("connectors") or {})),
    }


def merge_runtime_settings(yaml_sections: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, Any]:
    merged = _deep_copy_sections(yaml_sections)
    payload = overlay if isinstance(overlay, dict) else {}
    if "program_defaults" in payload and isinstance(payload.get("program_defaults"), dict):
        defaults = merged.setdefault("program_defaults", {})
        for key, value in payload["program_defaults"].items():
            if key in ALLOWED_DEFAULTS:
                defaults[key] = deepcopy(value)
        if "repo_picker" in defaults and isinstance(defaults["repo_picker"], dict):
            overlay_defaults = payload.get("program_defaults") if isinstance(payload.get("program_defaults"), dict) else {}
            overlay_picker = overlay_defaults.get("repo_picker") if isinstance(overlay_defaults.get("repo_picker"), dict) else {}
            baseline_defaults = yaml_sections.get("program_defaults") if isinstance(yaml_sections.get("program_defaults"), dict) else {}
            baseline_picker = baseline_defaults.get("repo_picker") if isinstance(baseline_defaults.get("repo_picker"), dict) else {}
            if "mode" in overlay_picker:
                mode = overlay_picker.get("mode")
            else:
                mode = baseline_picker.get("mode")
            repos = _canonicalize_repos(
                defaults["repo_picker"].get("repos") if isinstance(defaults["repo_picker"], dict) else [],
                path="$.program_defaults.repo_picker.repos",
                issues=[],
            )
            picker: dict[str, Any] = {"repos": repos}
            if isinstance(mode, str) and mode.strip():
                picker["mode"] = mode.strip()
            defaults["repo_picker"] = picker
        if "ticket_picker" in defaults and isinstance(defaults["ticket_picker"], dict):
            defaults["ticket_picker"]["assignee"] = "mapped_user"
    if "identity" in payload and isinstance(payload.get("identity"), dict):
        identity = merged.setdefault("identity", {})
        for key, value in payload["identity"].items():
            if key in ALLOWED_IDENTITY:
                identity[key] = deepcopy(value)
    for section, allowed in (
        ("policy", ALLOWED_POLICY),
        ("sandbox", ALLOWED_SANDBOX),
    ):
        if section in payload and isinstance(payload.get(section), dict):
            target = merged.setdefault(section, {})
            for key, value in payload[section].items():
                if key in allowed:
                    target[key] = deepcopy(value)
    if "bindings" in payload and isinstance(payload.get("bindings"), dict):
        merged.setdefault("bindings", {}).update(deepcopy(payload["bindings"]))
    if "toolsets" in payload and isinstance(payload.get("toolsets"), dict):
        merged.setdefault("toolsets", {}).update(deepcopy(payload["toolsets"]))
    if "connectors" in payload and isinstance(payload.get("connectors"), dict):
        google = payload["connectors"].get("google-workspace")
        if isinstance(google, dict):
            base = merged.setdefault("connectors", {}).setdefault("google-workspace", {})
            for key, value in google.items():
                if key not in ALLOWED_CONNECTOR:
                    continue
                if key in {"auth", "artifact_publish"} and isinstance(value, dict) and isinstance(base.get(key), dict):
                    child = deepcopy(base[key])
                    child.update(deepcopy(value))
                    base[key] = child
                else:
                    base[key] = deepcopy(value)
    return merged


def annotate_sources(yaml_sections: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, str]:
    del yaml_sections
    payload = overlay if isinstance(overlay, dict) else {}
    defaults = payload.get("program_defaults") if isinstance(payload.get("program_defaults"), dict) else {}
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    sources = {
        f"program_defaults.{key}": ("overlay" if key in defaults else "inherited")
        for key in ALLOWED_DEFAULTS
    }
    sources.update(
        {
            f"identity.{key}": ("overlay" if key in identity else "inherited")
            for key in ALLOWED_IDENTITY
        }
    )
    for section, allowed in (("policy", ALLOWED_POLICY), ("sandbox", ALLOWED_SANDBOX)):
        present = payload.get(section) if isinstance(payload.get(section), dict) else {}
        sources.update(
            {f"{section}.{key}": ("overlay" if key in present else "inherited") for key in allowed}
        )
    binding_overlay = payload.get("bindings") if isinstance(payload.get("bindings"), dict) else {}
    for key in set(binding_overlay):
        sources[f"bindings.{key}"] = "overlay"
    return sources


def _policy_section(config: Any) -> dict[str, Any]:
    policy = getattr(config, "policy", None)
    if policy is None:
        return {}
    return {key: deepcopy(getattr(policy, key)) for key in ALLOWED_POLICY}


def _sandbox_section(config: Any) -> dict[str, Any]:
    sandbox = getattr(config, "sandbox", None)
    if sandbox is None:
        return {}
    return {key: deepcopy(getattr(sandbox, key)) for key in ALLOWED_SANDBOX}


def _connector_section(config: Any) -> dict[str, Any]:
    connectors = getattr(config, "connectors", None)
    google = getattr(connectors, "google_workspace", None) if connectors else None
    if google is None:
        return {}
    auth = google.auth
    delegation = getattr(auth, "delegation", None) if auth else None
    oauth = getattr(auth, "oauth", None) if auth else None
    publish = google.artifact_publish
    library = publish.library
    return {
        "google-workspace": {
            "enabled": google.enabled,
            "auth": {
                "delegation": {
                    "credentials_secret": getattr(delegation, "credentials_secret", ""),
                    "default_impersonate_email": getattr(delegation, "default_impersonate_email", ""),
                },
                "oauth": {
                    "client_id_secret": getattr(oauth, "client_id_secret", ""),
                    "client_secret_secret": getattr(oauth, "client_secret_secret", ""),
                    "callback_url": getattr(oauth, "callback_url", ""),
                    "kms_key_id": getattr(oauth, "kms_key_id", ""),
                },
            },
            "accounts": {
                name: {"impersonate_email": account.impersonate_email}
                for name, account in (google.accounts or {}).items()
            },
            "scopes": list(google.scopes or []),
            "artifact_publish": {
                "enabled": publish.enabled,
                "folder_id": publish.folder_id,
                "min_chars": publish.min_chars,
                "chunk_chars": publish.chunk_chars,
                "library": {
                    "enabled": library.enabled,
                    "publish_on": library.publish_on,
                    "share_folder_with_requester": library.share_folder_with_requester,
                    "program_subfolder": library.program_subfolder,
                    "step_phase_map": deepcopy(library.step_phase_map),
                },
            },
        }
    }


def yaml_settings_baseline(config: Any) -> dict[str, Any]:
    identity = getattr(config, "identity", None)
    return {
        "program_defaults": deepcopy(dict(getattr(config, "program_defaults", None) or {})),
        "identity": {
            key: deepcopy(dict(getattr(identity, key, None) or {})) for key in ALLOWED_IDENTITY
        },
        "policy": _policy_section(config),
        "sandbox": _sandbox_section(config),
        "bindings": deepcopy(dict(getattr(config, "bindings", None) or {})),
        "toolsets": deepcopy(dict(getattr(config, "toolsets", None) or {})),
        "connectors": _connector_section(config),
    }


def restore_yaml_settings_baseline(config: Any, baseline: dict[str, Any] | None) -> None:
    if not baseline or config is None:
        return
    if "program_defaults" in baseline:
        config.program_defaults = deepcopy(dict(baseline.get("program_defaults") or {}))
    if "identity" in baseline:
        identity = baseline.get("identity") or {}
        for key in ALLOWED_IDENTITY:
            setattr(config.identity, key, deepcopy(dict(identity.get(key) or {})))
    if "policy" in baseline and getattr(config, "policy", None) is not None:
        for key, value in (baseline.get("policy") or {}).items():
            if key in ALLOWED_POLICY:
                setattr(config.policy, key, deepcopy(value))
    if "sandbox" in baseline and getattr(config, "sandbox", None) is not None:
        for key, value in (baseline.get("sandbox") or {}).items():
            if key in ALLOWED_SANDBOX:
                setattr(config.sandbox, key, deepcopy(value))
    if "bindings" in baseline:
        config.bindings = deepcopy(dict(baseline.get("bindings") or {}))
    if "toolsets" in baseline:
        config.toolsets = deepcopy(dict(baseline.get("toolsets") or {}))
    if "connectors" in baseline:
        _apply_connector(config, baseline.get("connectors") or {})


def _apply_connector(config: Any, connectors: dict[str, Any]) -> None:
    from ts_harness.config.schema import _parse_google_workspace_connector

    google = connectors.get("google-workspace")
    if not isinstance(google, dict) or getattr(config, "connectors", None) is None:
        return
    config.connectors.google_workspace = _parse_google_workspace_connector(google)


def apply_settings_to_config(config: Any, overlay: dict[str, Any] | None) -> None:
    yaml_sections = yaml_settings_baseline(config)
    merged = merge_runtime_settings(yaml_sections, overlay or {})
    config.program_defaults = deepcopy(merged.get("program_defaults") or {})
    identity = merged.get("identity") or {}
    for key in ALLOWED_IDENTITY:
        if key in identity:
            setattr(config.identity, key, deepcopy(dict(identity.get(key) or {})))
    for key, value in (merged.get("policy") or {}).items():
        if key in ALLOWED_POLICY and getattr(config, "policy", None) is not None:
            setattr(config.policy, key, deepcopy(value))
    for key, value in (merged.get("sandbox") or {}).items():
        if key in ALLOWED_SANDBOX and getattr(config, "sandbox", None) is not None:
            setattr(config.sandbox, key, deepcopy(value))
    if "bindings" in merged:
        config.bindings = deepcopy(dict(merged.get("bindings") or {}))
    if "toolsets" in merged:
        config.toolsets = deepcopy(dict(merged.get("toolsets") or {}))
    if merged.get("connectors"):
        _apply_connector(config, merged.get("connectors") or {})
    _publish_sandbox_limits(config)


def reload_runtime_settings_into_runtime(runtime: Any) -> bool:
    if runtime is None or not hasattr(runtime, "config"):
        return False
    baseline = getattr(runtime, "_yaml_settings_baseline", None)
    if baseline is None:
        baseline = yaml_settings_baseline(runtime.config)
        runtime._yaml_settings_baseline = baseline
    try:
        from ts_harness.workflows.runtime_settings_store import runtime_settings_store

        restore_yaml_settings_baseline(runtime.config, baseline)
        active = runtime_settings_store().get_active(DEFINITION_TYPE)
        apply_settings_to_config(runtime.config, active)
        _refresh_google_factory(runtime)
        return True
    except Exception:
        restore_yaml_settings_baseline(runtime.config, baseline)
        return False


def _publish_sandbox_limits(config: Any) -> None:
    sandbox = getattr(config, "sandbox", None)
    if sandbox is None:
        return
    from ts_harness.sandbox.docker_env import set_sandbox_limits

    set_sandbox_limits(
        {
            "timeout_seconds": int(getattr(sandbox, "timeout_seconds", 300) or 300),
            "egress_policy": str(getattr(sandbox, "egress_policy", "deny") or "deny"),
            "cpu_limit": float(getattr(sandbox, "cpu_limit", 1.0) or 1.0),
            "memory_mb": int(getattr(sandbox, "memory_mb", 512) or 512),
        }
    )


def _refresh_google_factory(runtime: Any) -> None:
    try:
        from ts_harness.connectors.registry import get_connector_registry
    except Exception:
        return
    registry = get_connector_registry()
    if registry is None:
        return
    connector = registry.get_connector("google-workspace")
    google = getattr(getattr(runtime.config, "connectors", None), "google_workspace", None)
    adopt = getattr(connector, "adopt_config", None)
    if google is not None and callable(adopt):
        adopt(google)


def redact_runtime_yaml(
    data: dict[str, Any],
    *,
    token: str = "",
    secret_values: list[str] | None = None,
) -> str:
    text = yaml.safe_dump(data, sort_keys=False)
    replacements = [str(token or "").strip()]
    replacements.extend(str(value or "").strip() for value in (secret_values or []))
    for value in replacements:
        if value:
            text = text.replace(value, "***")
    return text
