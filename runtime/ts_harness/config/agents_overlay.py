"""Validate and merge overlay-only specialist agents."""

from __future__ import annotations

from copy import deepcopy
import os
import re
from pathlib import Path
from typing import Any

from ts_harness.config.schema import (
    FORBIDDEN_GATE_MODELS,
    GATE_AGENT_IDS,
    AgentConfig,
    RuntimeConfig,
)
from ts_harness.skills.catalog import SkillCatalog

DEFINITION_TYPE = "agents"
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
ALLOWED_TOP = ("definition_type", "definition_version", "agents")
ALLOWED_AGENT = (
    "workspace",
    "model",
    "display_name",
    "soul",
    "skills",
    "toolsets",
    "tools",
    "connector_bindings",
    "default",
    "delegatable",
    "sandbox_mode",
)
SOUL_MAX_CHARS = 500


def _gate_model_issues(
    agent_id: str, obj: dict[str, Any], model_allowlist: dict[str, list[str]] | None
) -> list[dict[str, str]]:
    if agent_id not in GATE_AGENT_IDS or model_allowlist is None:
        return []
    allowed = [str(model) for model in (model_allowlist.get(agent_id) or [])]
    model = str(obj.get("model") or "")
    if model in allowed and model not in FORBIDDEN_GATE_MODELS:
        return []
    listed = ", ".join(allowed) or "(none)"
    return [
        _issue(
            "gate_model_not_allowed",
            f"{agent_id} model must be one of: {listed}",
            path=f"$.agents.{agent_id}.model",
        )
    ]


def _issue(code: str, message: str, *, path: str = "$") -> dict[str, str]:
    return {"severity": "error", "code": code, "path": path, "message": message}


def _unknown_keys(
    payload: dict[str, Any], allowed: tuple[str, ...], path: str
) -> list[dict[str, str]]:
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


def _soul_value(obj: dict[str, Any]) -> str | None:
    if "soul" not in obj:
        return None
    return str(obj.get("soul") or "").strip()[:SOUL_MAX_CHARS]


def overlay_object_to_agent(agent_id: str, obj: dict[str, Any]) -> AgentConfig:
    tools = obj.get("tools") if isinstance(obj.get("tools"), dict) else {}
    bindings = obj.get("connector_bindings") if isinstance(obj.get("connector_bindings"), dict) else {}
    return AgentConfig(
        id=agent_id,
        workspace=str(obj.get("workspace") or f"/workspace/{agent_id}"),
        model=str(obj.get("model") or ""),
        skills=list(obj.get("skills") or []),
        toolsets=list(obj.get("toolsets") or []),
        tool_allow=list((tools or {}).get("allow") or []),
        tool_deny=list((tools or {}).get("deny") or []),
        connector_bindings={str(k): str(v) for k, v in bindings.items()},
        soul=_soul_value(obj),
        default=False,
        sandbox_mode=str(obj.get("sandbox_mode") or "off"),
    )


def _catalog_tokens(catalog: SkillCatalog) -> set[str]:
    tokens: set[str] = set()
    for key, meta in catalog.index.items():
        tokens.add(str(key))
        tokens.add(meta.id)
        tokens.add(meta.category)
    return tokens


def validate_agents_overlay(
    payload: dict[str, Any] | None,
    *,
    yaml_agent_ids: set[str],
    toolset_keys: set[str] | None,
    catalog: SkillCatalog | None,
    tenant_id: str = "default",
    model_allowlist: dict[str, list[str]] | None = None,
    google_accounts: set[str] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    del yaml_agent_ids, tenant_id
    issues: list[dict[str, str]] = []
    if payload is None:
        return None, issues
    if not isinstance(payload, dict):
        return None, [_issue("invalid_document", "agents overlay must be an object")]
    issues.extend(_unknown_keys(payload, ALLOWED_TOP, "$"))
    if str(payload.get("definition_type") or "") != DEFINITION_TYPE:
        issues.append(
            _issue(
                "invalid_definition_type",
                "definition_type must be agents",
                path="$.definition_type",
            )
        )
    agents = payload.get("agents")
    if agents is None:
        agents = {}
    if not isinstance(agents, dict):
        issues.append(_issue("invalid_agents", "agents must be an object", path="$.agents"))
        return None, issues

    known_skills = _catalog_tokens(catalog) if catalog is not None else None
    for agent_id, obj in agents.items():
        path = f"$.agents.{agent_id}"
        if not AGENT_ID_RE.match(str(agent_id)):
            issues.append(
                _issue("invalid_agent_id", f"invalid agent id: {agent_id}", path=path)
            )
        if not isinstance(obj, dict):
            issues.append(_issue("invalid_agent", "agent must be an object", path=path))
            continue
        issues.extend(_unknown_keys(obj, ALLOWED_AGENT, path))
        expected_workspace = f"/workspace/{agent_id}"
        if str(obj.get("workspace") or "") != expected_workspace:
            issues.append(
                _issue(
                    "invalid_workspace",
                    f"workspace must be {expected_workspace}",
                    path=f"{path}.workspace",
                )
            )
        if obj.get("default") is True:
            issues.append(
                _issue(
                    "default_not_allowed",
                    "overlay agents must have default: false",
                    path=f"{path}.default",
                )
            )
        toolsets = obj.get("toolsets") or []
        if not isinstance(toolsets, list):
            issues.append(
                _issue("invalid_toolsets", "toolsets must be a list", path=f"{path}.toolsets")
            )
            toolsets = []
        if toolset_keys is not None:
            for toolset in toolsets:
                if str(toolset) not in toolset_keys:
                    issues.append(
                        _issue(
                            "unknown_toolset",
                            f"unknown toolset: {toolset}",
                            path=f"{path}.toolsets",
                        )
                    )
        skills = obj.get("skills")
        if not isinstance(skills, list) or not [item for item in skills if str(item).strip()]:
            issues.append(
                _issue(
                    "skills_required",
                    "overlay agents must assign at least one skill",
                    path=f"{path}.skills",
                )
            )
        elif known_skills is not None:
            for token in skills:
                if str(token) not in known_skills:
                    issues.append(
                        _issue(
                            "unresolvable_skill",
                            f"skill is not a live catalog id or category: {token}",
                            path=f"{path}.skills",
                        )
                    )
        issues.extend(_gate_model_issues(str(agent_id), obj, model_allowlist))
        bindings = obj.get("connector_bindings")
        if bindings is not None and not isinstance(bindings, dict):
            issues.append(
                _issue(
                    "invalid_connector_bindings",
                    "connector_bindings must be an object",
                    path=f"{path}.connector_bindings",
                )
            )
        elif isinstance(bindings, dict) and google_accounts is not None:
            account = str(bindings.get("google-workspace") or "").strip()
            if account and account not in google_accounts:
                issues.append(
                    _issue(
                        "unknown_google_account",
                        f"unknown Google account: {account}",
                        path=f"{path}.connector_bindings.google-workspace",
                    )
                )

    if any(issue.get("severity") == "error" for issue in issues):
        return None, issues
    canonical = {
        "definition_type": DEFINITION_TYPE,
        "definition_version": str(payload.get("definition_version") or "1.0.0"),
        "agents": deepcopy(agents),
    }
    return canonical, issues


def merge_agents(
    yaml_agents: list[AgentConfig],
    overlay: dict[str, Any] | None,
) -> list[AgentConfig]:
    merged = [deepcopy(agent) for agent in yaml_agents]
    by_id = {agent.id: index for index, agent in enumerate(merged)}
    agents = (overlay or {}).get("agents") or {}
    if not isinstance(agents, dict):
        return merged
    for agent_id, obj in agents.items():
        if not isinstance(obj, dict):
            continue
        agent = overlay_object_to_agent(str(agent_id), obj)
        index = by_id.get(agent.id)
        if index is None:
            by_id[agent.id] = len(merged)
            merged.append(agent)
        else:
            previous = merged[index]
            agent.default = previous.default
            agent.subagent_allow = list(previous.subagent_allow)
            agent.heartbeat = previous.heartbeat
            if "connector_bindings" not in obj:
                agent.connector_bindings = dict(previous.connector_bindings)
            if "soul" not in obj:
                agent.soul = previous.soul
            merged[index] = agent
    return merged


def catalog_souls(path: Path | None = None) -> dict[str, str]:
    from ts_harness.repo_paths import repo_root

    catalog_path = path or (repo_root() / "config" / "agents" / "catalog.yaml")
    if not catalog_path.is_file():
        return {}
    import yaml

    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    agents = raw.get("agents") if isinstance(raw, dict) else {}
    if not isinstance(agents, dict):
        return {}
    souls: dict[str, str] = {}
    for agent_id, obj in agents.items():
        if not isinstance(obj, dict):
            continue
        text = " ".join(str(obj.get("soul") or "").split())
        if text:
            souls[str(agent_id)] = text[:SOUL_MAX_CHARS]
    return souls


def fill_agent_souls(agents: list[AgentConfig], souls: dict[str, str] | None = None) -> None:
    lookup = catalog_souls() if souls is None else souls
    for agent in agents:
        if agent.soul is None:
            agent.soul = lookup.get(agent.id, "")


def apply_agents_to_config(
    config: RuntimeConfig,
    overlay: dict[str, Any] | None,
    *,
    baseline: list[AgentConfig],
) -> None:
    config.agents = merge_agents(baseline, overlay)
    fill_agent_souls(config.agents)
    overlay_agents = (overlay or {}).get("agents") or {}
    if not isinstance(overlay_agents, dict):
        return
    orchestrator = config.default_agent()
    for agent_id, obj in overlay_agents.items():
        if not isinstance(obj, dict):
            continue
        if obj.get("delegatable") is True and agent_id not in orchestrator.subagent_allow:
            orchestrator.subagent_allow.append(agent_id)


def write_overlay_agents_md(
    agent_id: str, obj: dict[str, Any], *, workspace_root: Path
) -> Path:
    from ts_harness.storage.workspace import workspace_store

    display_name = str(obj.get("display_name") or agent_id)
    soul = str(obj.get("soul") or "")
    model = str(obj.get("model") or "")
    store = workspace_store(agent_id, root=workspace_root)
    store.write_text(
        "AGENTS.md",
        (
            f"# {display_name} ({agent_id})\n\n"
            f"**Soul:** {soul}\n\n"
            f"**Model:** `{model}`\n"
            f"**Workspace:** `/workspace/{agent_id}`\n\n"
            "This specialist is overlay-managed. It is not defined in "
            "ts-harness-runtime.yaml.\n"
        ),
    )
    return Path(store.display_path("AGENTS.md"))


def _workspace_root(runtime: Any) -> Path:
    override = getattr(runtime, "agents_workspace_root", None)
    if override:
        return Path(override)
    env = os.environ.get("TS_HARNESS_WORKSPACE_ROOT", "").strip()
    if env:
        return Path(env)
    return Path("/workspace")


def reload_agents_overlay_into_runtime(runtime: Any) -> None:
    from ts_harness.workflows.agents_overlay_store import agents_overlay_store

    store = agents_overlay_store()
    active = store.get_active(DEFINITION_TYPE)
    baseline = getattr(runtime, "_yaml_agents_baseline", None) or list(
        runtime.config.agents
    )
    apply_agents_to_config(runtime.config, active, baseline=baseline)
    overlay_agents = (active or {}).get("agents") or {}
    if not isinstance(overlay_agents, dict):
        return
    yaml_ids = {agent.id for agent in baseline}
    root = _workspace_root(runtime)
    for agent_id, obj in overlay_agents.items():
        if agent_id in yaml_ids or not isinstance(obj, dict):
            continue
        write_overlay_agents_md(agent_id, obj, workspace_root=root)
