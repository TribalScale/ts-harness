#!/usr/bin/env python3
"""Render workspace markdown templates from organization.yaml.

Agent identity sections are generated separately by
``scripts/generate-agent-docs.py`` (runtime YAML + catalog overlay).
Run generate-agent-docs before this script (or rely on CI ``--check``)
so organization placeholder substitution does not fight generated structure.
"""

import argparse
import pathlib
import re
import shutil
import sys

import yaml


TOKEN_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


class ConfigError(ValueError):
    """Raised when organization.yaml cannot safely render workspace templates."""


def _get(mapping, key, default="-"):
    value = mapping.get(key, default)
    if value is None:
        return default
    return value


def _require(mapping, dotted_path):
    current = mapping
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ConfigError(f"missing required config value: {dotted_path}")
        current = current[part]
    if current is None or current == "" or current == []:
        raise ConfigError(f"empty required config value: {dotted_path}")
    return current


def _validate_list(config, name, max_items, required_keys):
    items = _require(config, name)
    if not isinstance(items, list):
        raise ConfigError(f"{name} must be a list")
    if len(items) > max_items:
        raise ConfigError(f"{name} supports at most {max_items} entries")
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"{name}[{index}] must be an object")
        for key in required_keys:
            _require(item, key)


def validate_config(config):
    """Validate only the fields needed by current workspace templates."""
    for path in (
        "organization.company_name",
        "organization.github_org",
        "organization.agent_name",
        "organization.timezone",
        "organization.expertise_areas",
        "channels.approved",
        "channels.standup",
        "channels.ops",
        "channels.dev",
        "channels.team",
        "channels.sales",
        "channels.marketing",
        "channels.leadership",
        "channels.admin",
        "tooling.monitoring_tool",
        "tooling.secrets_manager",
        "tooling.feature_flag_system",
        "tooling.config_location",
        "contacts.security_lead.name",
        "contacts.security_lead.contact",
        "contacts.qa_lead.name",
        "contacts.qa_lead.contact",
        "contacts.platform_admin.name",
        "contacts.platform_admin.contact",
        "contacts.engineering_lead.name",
        "environments.dev.url",
        "environments.staging.url",
        "environments.prod.url",
        "environments.preview_url_pattern",
    ):
        _require(config, path)

    _validate_list(config, "teams", 4, ("name", "lead", "members", "repo", "jira", "slack"))
    _validate_list(config, "systems", 5, ("name", "purpose", "stack", "host", "owner"))
    _validate_list(config, "glossary", 5, ("term", "definition"))
    _validate_list(config, "runbooks", 5, ("name", "desc", "location"))


def _indexed(items, count):
    padded = list(items or [])
    while len(padded) < count:
        padded.append({})
    return padded[:count]


def jira_projects(config):
    """Jira project keys from organization.jira_projects, else team jira keys."""
    org = config.get("organization", {}) or {}
    keys = [str(key).strip() for key in org.get("jira_projects") or [] if str(key or "").strip()]
    if not keys:
        for team in config.get("teams", []) or []:
            key = str((team or {}).get("jira") or "").strip()
            if key and key != "-" and key not in keys:
                keys.append(key)
    return keys


def flatten(config):
    """Return a placeholder-to-value map for workspace rendering."""
    org = config.get("organization", {})
    projects = jira_projects(config)
    channels = config.get("channels", {})
    tooling = config.get("tooling", {})
    contacts = config.get("contacts", {})
    environments = config.get("environments", {})

    security_lead = contacts.get("security_lead", {})
    qa_lead = contacts.get("qa_lead", {})
    platform_admin = contacts.get("platform_admin", {})
    engineering_lead = contacts.get("engineering_lead", {})

    out = {
        "COMPANY_NAME": _get(org, "company_name"),
        "GITHUB_ORG": _get(org, "github_org"),
        "JIRA_PROJECTS": ", ".join(projects) or "-",
        "JIRA_PRIMARY_PROJECT": projects[0] if projects else "PROJ",
        "AGENT_NAME": _get(org, "agent_name"),
        "AGENT_TIMEZONE": _get(org, "timezone"),
        "TIMEZONE": _get(org, "timezone"),
        "EXPERTISE_AREAS": ", ".join(org.get("expertise_areas", []) or ["-"]),
        "PLACEHOLDER": "PLACEHOLDER",
        "APPROVED_CHANNELS": ", ".join(channels.get("approved", []) or ["-"]),
        "STANDUP_CHANNEL": _get(channels, "standup"),
        "OPS_CHANNEL": _get(channels, "ops"),
        "DEV_CHANNEL": _get(channels, "dev"),
        "TEAM_CHANNEL": _get(channels, "team"),
        "SALES_CHANNEL": _get(channels, "sales"),
        "MARKETING_CHANNEL": _get(channels, "marketing"),
        "LEADERSHIP_CHANNEL": _get(channels, "leadership"),
        "ADMIN_CHANNEL": _get(channels, "admin"),
        "MONITORING_TOOL": _get(tooling, "monitoring_tool"),
        "SECRETS_MANAGER": _get(tooling, "secrets_manager"),
        "FEATURE_FLAG_SYSTEM": _get(tooling, "feature_flag_system"),
        "CONFIG_LOCATION": _get(tooling, "config_location"),
        "SECURITY_LEAD": _get(security_lead, "name"),
        "SEC_LEAD_CONTACT": _get(security_lead, "contact"),
        "QA_LEAD": _get(qa_lead, "name"),
        "QA_LEAD_CONTACT": _get(qa_lead, "contact"),
        "PLATFORM_ADMIN": _get(platform_admin, "name"),
        "ADMIN_CONTACT": _get(platform_admin, "contact"),
        "ENGINEERING_LEAD": _get(engineering_lead, "name"),
        "DEV_URL": _get(environments.get("dev", {}), "url"),
        "STAGING_URL": _get(environments.get("staging", {}), "url"),
        "PROD_URL": _get(environments.get("prod", {}), "url"),
        "PREVIEW_URL_PATTERN": _get(environments, "preview_url_pattern"),
    }

    for i, team in enumerate(_indexed(config.get("teams", []), 4), 1):
        for key in ("name", "lead", "members", "repo", "jira", "slack"):
            out[f"TEAM_{i}_{key.upper()}"] = _get(team, key)

    for i, system in enumerate(_indexed(config.get("systems", []), 5), 1):
        for key in ("name", "purpose", "stack", "host", "owner"):
            out[f"SYSTEM_{i}_{key.upper()}"] = _get(system, key)

    for i, item in enumerate(_indexed(config.get("glossary", []), 5), 1):
        out[f"TERM_{i}"] = _get(item, "term")
        out[f"DEFINITION_{i}"] = _get(item, "definition")

    for i, runbook in enumerate(_indexed(config.get("runbooks", []), 5), 1):
        out[f"RUNBOOK_{i}_NAME"] = _get(runbook, "name")
        out[f"RUNBOOK_{i}_DESC"] = _get(runbook, "desc")
        out[f"RUNBOOK_{i}_LOCATION"] = _get(runbook, "location")

    return {key: str(value) for key, value in out.items()}


def render_text(text, values):
    missing = set()

    def substitute(match):
        key = match.group(1)
        if key in values:
            return values[key]
        missing.add(key)
        return match.group(0)

    return TOKEN_RE.sub(substitute, text), missing


def render_tree(src, dst, values):
    missing = set()
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    for source in src.rglob("*"):
        target = dst / source.relative_to(src)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".md":
            rendered, unresolved = render_text(source.read_text(), values)
            target.write_text(rendered)
            missing.update(unresolved)
        else:
            shutil.copy2(source, target)

    return missing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--src", required=True)
    parser.add_argument("--dst", required=True)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any workspace markdown placeholder remains unresolved.",
    )
    args = parser.parse_args()

    config_path = pathlib.Path(args.config)
    src = pathlib.Path(args.src)
    dst = pathlib.Path(args.dst)

    config = yaml.safe_load(config_path.read_text()) or {}
    try:
        validate_config(config)
        values = flatten(config)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    missing = render_tree(src, dst, values)

    if missing:
        print(f"WARNING: unresolved placeholders: {sorted(missing)}", file=sys.stderr)
        if args.strict:
            return 1

    print(f"Rendered {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
