"""Admin BFF for overlay-only specialist agents."""

from __future__ import annotations

from typing import Any

from ts_harness.config.agents_overlay import (
    DEFINITION_TYPE,
    _workspace_root,
    apply_agents_to_config,
    overlay_object_to_agent,
    validate_agents_overlay,
    write_overlay_agents_md,
)
from ts_harness.gateway.auth import AuthContext
from ts_harness.connectors.tool_names import registered_google_workspace_tool_names
from ts_harness.tools.mcp_registry import registered_mcp_tool_names
from ts_harness.workflows.agents_overlay_store import agents_overlay_store


def _require_body_object(body: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("JSON object body required")
    return body


def _optional_editor_metadata(body: dict[str, Any]) -> dict[str, Any] | None:
    if "editor_metadata" not in body:
        return None
    metadata = body.get("editor_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("editor_metadata must be an object")
    return metadata


def _empty_definition() -> dict[str, Any]:
    return {
        "definition_type": DEFINITION_TYPE,
        "definition_version": "1.0.0",
        "agents": {},
    }


def _baseline(runtime: Any) -> list[Any]:
    return list(getattr(runtime, "_yaml_agents_baseline", None) or runtime.config.agents)


def _overlay_ids(definition: dict[str, Any] | None) -> set[str]:
    agents = (definition or {}).get("agents") or {}
    if not isinstance(agents, dict):
        return set()
    return {str(agent_id) for agent_id in agents}


def _validation_args(runtime: Any) -> dict[str, Any]:
    catalog = getattr(runtime, "skill_catalog", None)
    if catalog is None:
        from ts_harness.repo_paths import repo_root
        from ts_harness.skills.catalog import SkillCatalog
        from ts_harness.skills.roots import managed_root

        roots = [repo_root() / "skills"]
        if managed_root().resolve() != roots[0].resolve():
            roots.append(managed_root())
        catalog = SkillCatalog(roots=roots)
        catalog.load()
    return {
        "yaml_agent_ids": {agent.id for agent in _baseline(runtime)},
        "toolset_keys": set(runtime.config.toolsets),
        "catalog": catalog,
        "model_allowlist": dict(runtime.config.policy.model_allowlist),
        "google_accounts": _google_accounts(runtime),
    }


def _google_accounts(runtime: Any) -> set[str]:
    connectors = getattr(runtime.config, "connectors", None)
    google = getattr(connectors, "google_workspace", None) if connectors else None
    accounts = getattr(google, "accounts", None) or {}
    return {str(key) for key in accounts}


def _agent_row(agent: Any, *, source: str) -> dict[str, Any]:
    return {
        "id": agent.id,
        "source": source,
        "default": agent.default,
        "model": agent.model,
        "skills": list(agent.skills),
        "toolsets": list(agent.toolsets),
        "tools": {"allow": list(agent.tool_allow), "deny": list(agent.tool_deny)},
        "connector_bindings": dict(agent.connector_bindings),
        "soul": agent.soul or "",
    }


def get_agents_admin(runtime: Any) -> dict[str, Any]:
    store = agents_overlay_store()
    active = store.get_active(DEFINITION_TYPE)
    drafts = store.list_drafts()
    draft = drafts[0] if drafts else None
    overlay = (draft or {}).get("definition") if draft else (active or {})
    if not isinstance(overlay, dict):
        overlay = {}
    overlay_ids = _overlay_ids(overlay)
    roster = [
        _agent_row(agent, source="overlay" if agent.id in overlay_ids else "inherited")
        for agent in runtime.config.agents
    ]
    live_ids = {row["id"] for row in roster}
    overlay_agents = overlay.get("agents") or {}
    if isinstance(overlay_agents, dict):
        for agent_id, obj in overlay_agents.items():
            if agent_id in live_ids or not isinstance(obj, dict):
                continue
            tools = obj.get("tools") if isinstance(obj.get("tools"), dict) else {}
            roster.append(
                {
                    "id": str(agent_id),
                    "source": "overlay",
                    "default": False,
                    "model": obj.get("model"),
                    "skills": list(obj.get("skills") or []),
                    "toolsets": list(obj.get("toolsets") or []),
                    "tools": {
                        "allow": list(tools.get("allow") or []),
                        "deny": list(tools.get("deny") or []),
                    },
                    "connector_bindings": dict(obj.get("connector_bindings") or {}),
                    "soul": str(obj.get("soul") or ""),
                }
            )
    return {
        "status": "active" if active else "empty",
        "definition": active or _empty_definition(),
        "draft": draft,
        "active_revision": store.get_active_revision(DEFINITION_TYPE),
        "roster": roster,
        "toolsets": list(runtime.config.toolsets),
        "tool_catalog": {name: list(tools) for name, tools in runtime.config.toolsets.items()},
        "discovered_tools": {
            "mcp": sorted(registered_mcp_tool_names()),
            "google-workspace": sorted(registered_google_workspace_tool_names()),
        },
        "google_accounts": sorted(_google_accounts(runtime)),
        "yaml_agent_ids": [agent.id for agent in _baseline(runtime)],
    }


def create_agents_draft_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    del runtime
    body = _require_body_object(body)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    draft = agents_overlay_store().create_draft(
        definition,
        actor_id=auth.actor_id,
        editor_metadata=_optional_editor_metadata(body),
    )
    return {"draft": draft}


def update_agents_draft_admin(
    runtime: Any,
    auth: AuthContext,
    draft_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    del runtime
    body = _require_body_object(body)
    definition = body.get("definition")
    expected_revision = str(body.get("expected_revision") or "")
    if not expected_revision:
        raise ValueError("expected_revision is required")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    update_kwargs: dict[str, Any] = {}
    if "editor_metadata" in body:
        update_kwargs["editor_metadata"] = _optional_editor_metadata(body)
    draft = agents_overlay_store().update_draft(
        draft_id,
        definition,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
        **update_kwargs,
    )
    return {"draft": draft}


def validate_agents_draft_admin(
    runtime: Any, draft_id: str, *, tenant_id: str = "default"
) -> dict[str, Any]:
    draft = agents_overlay_store().get_draft(draft_id)
    canonical, issues = validate_agents_overlay(
        draft.get("definition") or {},
        **_validation_args(runtime),
    )
    if canonical is not None:
        try:
            _fail_closed_resolve(runtime, canonical, tenant_id=tenant_id)
        except ValueError as exc:
            issues = [
                *issues,
                {
                    "severity": "error",
                    "code": "unresolvable_overlay_agent",
                    "path": "$.agents",
                    "message": str(exc),
                },
            ]
            canonical = None
    return {
        "valid": not any(issue.get("severity") == "error" for issue in issues),
        "issues": issues,
        "normalized": canonical,
    }


def _fail_closed_resolve(runtime: Any, overlay: dict[str, Any], *, tenant_id: str) -> None:
    catalog = _validation_args(runtime)["catalog"]
    agents = overlay.get("agents") or {}
    if not isinstance(agents, dict):
        return
    for agent_id, obj in agents.items():
        if not isinstance(obj, dict):
            continue
        agent = overlay_object_to_agent(str(agent_id), obj)
        resolved, _diagnostics = catalog.resolve_for_agent(
            agent, runtime.config, tenant_id=tenant_id
        )
        if not resolved:
            raise ValueError(f"unresolvable overlay agent: {agent_id}")


def activate_agents_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    draft_id = str(body.get("draft_id") or "").strip()
    expected_revision = str(body.get("expected_revision") or "")
    if not draft_id:
        raise ValueError("draft_id is required")
    if not expected_revision:
        raise ValueError("expected_revision is required")
    draft = agents_overlay_store().get_draft(draft_id)
    canonical, issues = validate_agents_overlay(
        draft.get("definition") or {},
        **_validation_args(runtime),
    )
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        raise ValueError(
            "; ".join(issue["message"] for issue in errors) or "agents overlay validation failed"
        )
    _fail_closed_resolve(runtime, canonical, tenant_id=auth.tenant_id)
    activated = agents_overlay_store().activate(
        draft_id,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
    )
    apply_agents_to_config(
        runtime.config, canonical, baseline=_baseline(runtime)
    )
    yaml_ids = {agent.id for agent in _baseline(runtime)}
    root = _workspace_root(runtime)
    for agent_id, obj in (canonical.get("agents") or {}).items():
        if agent_id in yaml_ids or not isinstance(obj, dict):
            continue
        write_overlay_agents_md(agent_id, obj, workspace_root=root)
    return {
        "activated": activated,
        "issues": issues,
        "reload_ok": True,
    }
