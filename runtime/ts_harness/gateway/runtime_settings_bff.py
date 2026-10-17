"""Admin BFF for managed runtime-settings overlays."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ts_harness.config.runtime_settings import (
    DEFINITION_TYPE,
    annotate_sources,
    apply_settings_to_config,
    merge_runtime_settings,
    redact_runtime_yaml,
    restore_yaml_settings_baseline,
    validate_runtime_settings_payload,
    yaml_settings_baseline,
)
from ts_harness.gateway.auth import AuthContext
from ts_harness.routing.overlay import (
    apply_overlay_to_routing,
    routing_config_as_dicts,
)
from ts_harness.workflows.runtime_settings_store import (
    runtime_settings_store,
)


def _settings_validation_kwargs(runtime: Any) -> dict[str, Any]:
    from ts_harness.connectors.tool_names import registered_google_workspace_tool_names
    from ts_harness.tools.mcp_registry import registered_mcp_tool_names
    from ts_harness.tools.toolsets import TOOLSET_DEFINITIONS

    config = runtime.config
    agent_ids = {str(agent.id) for agent in getattr(config, "agents", [])}
    tool_names: set[str] = set()
    for names in TOOLSET_DEFINITIONS.values():
        tool_names.update(names)
    for names in getattr(config, "toolsets", {}).values():
        tool_names.update(str(name) for name in names or [])
    tool_names.update(registered_mcp_tool_names())
    tool_names.update(registered_google_workspace_tool_names())
    registry = getattr(runtime, "tool_registry", None)
    snapshot = getattr(registry, "snapshot", None)
    if callable(snapshot):
        tool_names.update(str(name) for name in snapshot())
    return {"known_agent_ids": agent_ids, "known_tool_names": tool_names}


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
    }


def _settings_document(runtime: Any, settings_sections: dict[str, Any]) -> dict[str, Any]:
    routing = getattr(getattr(runtime, "config", None), "routing", None)
    gateway = getattr(getattr(runtime, "config", None), "gateway", None)
    return {
        "gateway": {
            "token": getattr(gateway, "token", ""),
            "single_tenant_mode": getattr(gateway, "single_tenant_mode", True),
        },
        "identity": deepcopy(settings_sections.get("identity") or {}),
        "program_defaults": deepcopy(settings_sections.get("program_defaults") or {}),
        "routing": {
            "default": getattr(routing, "default", "ad_hoc_delegate"),
            "intents": routing_config_as_dicts(routing),
        },
    }


def _secret_values(runtime: Any) -> list[str]:
    resolver = getattr(runtime, "secret_resolver", None)
    if resolver is None:
        return []
    return [str(value) for value in getattr(resolver, "_cache", {}).values() if value]


def _redact_view(runtime: Any, settings_sections: dict[str, Any]) -> str:
    token = str(getattr(getattr(runtime.config, "gateway", None), "token", "") or "")
    return redact_runtime_yaml(
        _settings_document(runtime, settings_sections),
        token=token,
        secret_values=_secret_values(runtime),
    )


def get_runtime_settings_admin(runtime: Any) -> dict[str, Any]:
    store = runtime_settings_store()
    active = store.get_active(DEFINITION_TYPE)
    drafts = store.list_drafts()
    draft = drafts[0] if drafts else None
    baseline = getattr(runtime, "_yaml_settings_baseline", None) or yaml_settings_baseline(
        runtime.config
    )
    overlay = (draft or {}).get("definition") if draft else (active or {})
    if not isinstance(overlay, dict):
        overlay = {}
    merged = merge_runtime_settings(baseline, overlay)
    live = yaml_settings_baseline(runtime.config)
    return {
        "status": "active" if active else "empty",
        "definition": active or _empty_definition(),
        "draft": draft,
        "active_revision": store.get_active_revision(DEFINITION_TYPE),
        "yaml": baseline,
        "merged": merged,
        "sources": annotate_sources(baseline, overlay),
        "redacted_yaml_draft": _redact_view(runtime, merged),
        "redacted_yaml_live": _redact_view(runtime, live),
    }


def create_runtime_settings_draft_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    del runtime
    body = _require_body_object(body)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    draft = runtime_settings_store().create_draft(
        definition,
        actor_id=auth.actor_id,
        editor_metadata=_optional_editor_metadata(body),
    )
    return {"draft": draft}


def get_runtime_settings_draft_admin(draft_id: str) -> dict[str, Any]:
    return {"draft": runtime_settings_store().get_draft(draft_id)}


def update_runtime_settings_draft_admin(
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
    draft = runtime_settings_store().update_draft(
        draft_id,
        definition,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
        **update_kwargs,
    )
    return {"draft": draft}


def validate_runtime_settings_draft_admin(runtime: Any, draft_id: str) -> dict[str, Any]:
    draft = runtime_settings_store().get_draft(draft_id)
    canonical, issues = validate_runtime_settings_payload(
        draft.get("definition") or {},
        **_settings_validation_kwargs(runtime),
    )
    return {
        "valid": not any(issue.get("severity") == "error" for issue in issues),
        "issues": issues,
        "normalized": canonical,
    }


def guard_governance_change(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
    canonical: dict[str, Any] | None,
    *,
    memory_client: Any = None,
) -> dict[str, Any] | None:
    """Require a tenant admin and a reason to change governance, and audit it.

    Returns the recorded change, or None when governance values are unchanged.
    """
    from ts_harness.config.governance import governance_snapshot
    from ts_harness.gateway.admin_bff import is_admin

    if canonical is None:
        return None
    baseline = getattr(runtime, "_yaml_settings_baseline", None) or yaml_settings_baseline(
        runtime.config
    )
    before = governance_snapshot(getattr(runtime.config, "program_defaults", None))
    merged = merge_runtime_settings(baseline, canonical)
    after = governance_snapshot(merged.get("program_defaults"))
    if before == after:
        return None
    if not is_admin(auth, runtime):
        raise PermissionError("only a tenant admin can change governance settings")
    reason = str(body.get("governance_reason") or "").strip()
    if not reason:
        raise ValueError("governance_reason is required to change governance settings")
    enabled = after["independent_review"]["enabled"]
    client = memory_client
    if client is None:
        from ts_harness.memory.client import get_memory_client

        client = get_memory_client()
    try:
        recorded = client.record_decision(
            kind="governance_setting",
            decision=f"independent_review.enabled={'true' if enabled else 'false'}",
            actor_id=str(auth.actor_id or "admin"),
            tenant_id=str(auth.tenant_id or "default"),
            actor_email=str(auth.email or "").strip().lower() or None,
            actor_roles=[str(role) for role in (auth.roles or [])],
            reason=reason,
            details={"before": before, "after": after},
        )
    except Exception as exc:
        raise ValueError(f"governance change not recorded; activation aborted: {exc}") from exc
    return {"before": before, "after": after, "decision": recorded}


def _apply_settings_draft(runtime: Any, definition: dict[str, Any]) -> None:
    baseline = getattr(runtime, "_yaml_settings_baseline", None) or yaml_settings_baseline(
        runtime.config
    )
    restore_yaml_settings_baseline(runtime.config, baseline)
    apply_settings_to_config(runtime.config, definition)


def activate_runtime_settings_draft_admin(
    runtime: Any,
    auth: AuthContext,
    draft_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    expected_revision = str(body.get("expected_revision") or "")
    if not expected_revision:
        raise ValueError("expected_revision is required")
    draft = runtime_settings_store().get_draft(draft_id)
    canonical, issues = validate_runtime_settings_payload(
        draft.get("definition") or {},
        **_settings_validation_kwargs(runtime),
    )
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        raise ValueError(
            "; ".join(issue["message"] for issue in errors) or "runtime settings validation failed"
        )
    governance_change = guard_governance_change(runtime, auth, body, canonical)
    try:
        _apply_settings_draft(runtime, canonical)
    except Exception:
        restore_yaml_settings_baseline(
            runtime.config,
            getattr(runtime, "_yaml_settings_baseline", None),
        )
        return {"activated": None, "issues": issues, "reload_ok": False}
    activated = runtime_settings_store().activate(
        draft_id,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
    )
    result = {"activated": activated, "issues": issues, "reload_ok": True}
    if governance_change:
        result["governance_change"] = governance_change
    return result


def activate_runtime_settings_page_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    settings_id = str(body.get("runtime_settings_draft_id") or "").strip() or None
    routing_id = str(body.get("routing_draft_id") or "").strip() or None
    if not settings_id and not routing_id:
        raise ValueError("runtime_settings_draft_id or routing_draft_id is required")
    issues: list[dict[str, str]] = []
    settings_canonical: dict[str, Any] | None = None
    routing_intents = None
    if settings_id:
        settings_rev = str(
            body.get("runtime_settings_expected_revision") or body.get("expected_revision") or ""
        )
        if not settings_rev:
            raise ValueError("runtime_settings_expected_revision is required")
        draft = runtime_settings_store().get_draft(settings_id)
        settings_canonical, settings_issues = validate_runtime_settings_payload(
            draft.get("definition") or {},
            **_settings_validation_kwargs(runtime),
        )
        issues.extend(settings_issues)
        if any(issue.get("severity") == "error" for issue in settings_issues) or settings_canonical is None:
            raise ValueError(
                "; ".join(
                    issue["message"]
                    for issue in settings_issues
                    if issue.get("severity") == "error"
                )
                or "runtime settings validation failed"
            )
    if routing_id:
        routing_rev = str(body.get("routing_expected_revision") or "")
        if not routing_rev:
            raise ValueError("routing_expected_revision is required")
        from ts_harness.gateway.routing_definitions_bff import preview_routing_from_draft

        routing_intents, routing_issues, _canonical = preview_routing_from_draft(routing_id)
        issues.extend(routing_issues)
        if any(issue.get("severity") == "error" for issue in routing_issues):
            raise ValueError(
                "; ".join(
                    issue["message"]
                    for issue in routing_issues
                    if issue.get("severity") == "error"
                )
                or "routing validation failed"
            )
    governance_change = guard_governance_change(runtime, auth, body, settings_canonical)
    settings_baseline = getattr(runtime, "_yaml_settings_baseline", None) or yaml_settings_baseline(
        runtime.config
    )
    routing_baseline = getattr(runtime, "_yaml_routing_baseline", None)
    previous_routing = runtime.config.routing
    try:
        restore_yaml_settings_baseline(runtime.config, settings_baseline)
        if settings_canonical is not None:
            apply_settings_to_config(runtime.config, settings_canonical)
        if routing_id is not None:
            runtime.config.routing = apply_overlay_to_routing(
                routing_baseline, routing_intents or []
            )
    except Exception:
        restore_yaml_settings_baseline(runtime.config, settings_baseline)
        runtime.config.routing = previous_routing
        return {"activated": None, "issues": issues, "reload_ok": False}

    activated: dict[str, Any] = {}
    if settings_id:
        activated["runtime_settings"] = runtime_settings_store().activate(
            settings_id,
            expected_revision=str(
                body.get("runtime_settings_expected_revision")
                or body.get("expected_revision")
                or ""
            ),
            actor_id=auth.actor_id,
        )
    if routing_id:
        from ts_harness.workflows.routing_definition_store import (
            routing_definition_store,
        )

        activated["routing"] = routing_definition_store().activate(
            routing_id,
            expected_revision=str(body.get("routing_expected_revision") or ""),
            actor_id=auth.actor_id,
        )
    result = {"activated": activated, "issues": issues, "reload_ok": True}
    if governance_change:
        result["governance_change"] = governance_change
    return result
