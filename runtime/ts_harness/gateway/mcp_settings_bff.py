"""Admin BFF for managed MCP settings overlays."""

from __future__ import annotations

from typing import Any

from ts_harness.config.mcp_settings import (
    DEFINITION_TYPE,
    annotate_mcp_sources,
    apply_mcp_to_config,
    load_mcp_catalog,
    merge_mcp_servers,
    redact_mcp_server,
    resolve_mcp_secret_refs,
    validate_mcp_settings_payload,
    yaml_mcp_baseline,
)
from ts_harness.gateway.auth import AuthContext
from ts_harness.tools.mcp_client import MCPClientManager
from ts_harness.workflows.mcp_settings_store import mcp_settings_store


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
        "servers": {},
    }


def _baseline(runtime: Any) -> dict[str, Any]:
    return getattr(runtime, "_yaml_mcp_baseline", None) or yaml_mcp_baseline(runtime.config)


def _current_overlay(store: Any) -> dict[str, Any]:
    drafts = store.list_drafts()
    draft = drafts[0] if drafts else None
    if draft and isinstance(draft.get("definition"), dict):
        return draft["definition"]
    active = store.get_active(DEFINITION_TYPE) or {}
    return active if isinstance(active, dict) else {}


def get_mcp_settings_admin(runtime: Any) -> dict[str, Any]:
    store = mcp_settings_store()
    active = store.get_active(DEFINITION_TYPE)
    drafts = store.list_drafts()
    draft = drafts[0] if drafts else None
    baseline = _baseline(runtime)
    overlay = (draft or {}).get("definition") if draft else (active or {})
    if not isinstance(overlay, dict):
        overlay = {}
    merged = merge_mcp_servers(baseline, overlay)
    redacted = {name: redact_mcp_server(server) for name, server in merged.items()}
    return {
        "status": "active" if active else "empty",
        "definition": active or _empty_definition(),
        "draft": draft,
        "active_revision": store.get_active_revision(DEFINITION_TYPE),
        "yaml": {name: redact_mcp_server(server) for name, server in baseline.items()},
        "merged": redacted,
        "sources": annotate_mcp_sources(baseline, overlay),
        "catalog": load_mcp_catalog(),
    }


def create_mcp_settings_draft_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    del runtime
    body = _require_body_object(body)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    draft = mcp_settings_store().create_draft(
        definition,
        actor_id=auth.actor_id,
        editor_metadata=_optional_editor_metadata(body),
    )
    return {"draft": draft}


def get_mcp_settings_draft_admin(draft_id: str) -> dict[str, Any]:
    return {"draft": mcp_settings_store().get_draft(draft_id)}


def update_mcp_settings_draft_admin(
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
    draft = mcp_settings_store().update_draft(
        draft_id,
        definition,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
        **update_kwargs,
    )
    return {"draft": draft}


def validate_mcp_settings_draft_admin(draft_id: str) -> dict[str, Any]:
    draft = mcp_settings_store().get_draft(draft_id)
    canonical, issues = validate_mcp_settings_payload(draft.get("definition") or {})
    return {
        "valid": not any(issue.get("severity") == "error" for issue in issues),
        "issues": issues,
        "normalized": canonical,
    }


def activate_mcp_settings_admin(
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
    draft = mcp_settings_store().get_draft(draft_id)
    canonical, issues = validate_mcp_settings_payload(draft.get("definition") or {})
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        raise ValueError(
            "; ".join(issue["message"] for issue in errors) or "mcp settings validation failed"
        )
    activated = mcp_settings_store().activate(
        draft_id,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
    )
    apply_mcp_to_config(runtime.config, canonical, baseline=_baseline(runtime))
    rebuilt = runtime.rebuild_mcp_runtime()
    return {
        "activated": activated,
        "issues": issues,
        "degraded": list(rebuilt.get("degraded") or []),
        "reload_ok": True,
    }


def get_mcp_catalog_admin() -> dict[str, Any]:
    return {"entries": load_mcp_catalog()}


def probe_mcp_server_admin(runtime: Any, name: str) -> dict[str, Any]:
    overlay = _current_overlay(mcp_settings_store())
    merged = merge_mcp_servers(_baseline(runtime), overlay)
    server = merged.get(name)
    if not isinstance(server, dict):
        raise KeyError(f"unknown mcp server: {name}")
    resolved = resolve_mcp_secret_refs(server, getattr(runtime, "secret_resolver", None))
    manager = MCPClientManager()
    live = runtime.mcp
    try:
        manager.register_server(name, resolved)
        tools = manager.discover_tools(name)
        state = manager.server_state(name)
        error = None
        if not getattr(state, "healthy", True):
            error = str((state.config or {}).get("last_error") or "discovery failed")
        return {"ok": bool(getattr(state, "healthy", False)), "tools": tools, "error": error}
    finally:
        manager.close_all()
        runtime.mcp = live
