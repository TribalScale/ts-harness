"""Admin BFF for managed channel routing overlays."""

from __future__ import annotations

from typing import Any

from ts_harness.gateway.auth import AuthContext
from ts_harness.routing.overlay import (
    apply_overlay_to_routing,
    intent_to_dict,
    overlay_intents_from_payload,
    validate_overlay_payload,
)
from ts_harness.workflows.materialize import list_skeleton_types
from ts_harness.workflows.program_registry import get_program_definition_registry
from ts_harness.workflows.routing_definition_store import (
    routing_definition_store,
)


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


def _known_catalogs() -> tuple[set[str], set[str]]:
    programs = set(get_program_definition_registry().list_types())
    workflows = set(list_skeleton_types())
    return programs, workflows


def reload_routing_into_runtime(runtime: Any) -> bool:
    """Merge active overlay into runtime.config.routing; fail-closed on error."""
    if runtime is None or not hasattr(runtime, "config"):
        return False
    baseline = getattr(runtime, "_yaml_routing_baseline", None)
    if baseline is None:
        baseline = runtime.config.routing
        runtime._yaml_routing_baseline = baseline
    try:
        active = routing_definition_store().get_active("channel_routing")
        overlay_intents = overlay_intents_from_payload(active)
        runtime.config.routing = apply_overlay_to_routing(baseline, overlay_intents)
        return True
    except Exception:
        return False


def get_routing_definition_admin(runtime: Any) -> dict[str, Any]:
    store = routing_definition_store()
    active = store.get_active("channel_routing")
    baseline = getattr(runtime, "_yaml_routing_baseline", None)
    routing = baseline if baseline is not None else getattr(runtime.config, "routing", None)
    yaml_intents = (
        [intent_to_dict(rule) for rule in routing.intents]
        if routing is not None
        else []
    )
    merged = (
        [intent_to_dict(rule) for rule in runtime.config.routing.intents]
        if getattr(runtime, "config", None) and runtime.config.routing
        else yaml_intents
    )
    drafts = store.list_drafts()
    draft = drafts[0] if drafts else None
    return {
        "status": "active" if active else "empty",
        "definition": active
        or {
            "definition_type": "channel_routing",
            "definition_version": "0.0.0",
            "intents": [],
        },
        "draft": draft,
        "active_revision": store.get_active_revision("channel_routing"),
        "yaml_intents": yaml_intents,
        "merged_intents": merged,
    }


def create_routing_definition_draft_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    del runtime
    body = _require_body_object(body)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    draft = routing_definition_store().create_draft(
        definition,
        actor_id=auth.actor_id,
        editor_metadata=_optional_editor_metadata(body),
    )
    return {"draft": draft}


def get_routing_definition_draft_admin(draft_id: str) -> dict[str, Any]:
    return {"draft": routing_definition_store().get_draft(draft_id)}


def update_routing_definition_draft_admin(
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
    draft = routing_definition_store().update_draft(
        draft_id,
        definition,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
        **update_kwargs,
    )
    return {"draft": draft}


def validate_routing_definition_draft_admin(draft_id: str) -> dict[str, Any]:
    draft = routing_definition_store().get_draft(draft_id)
    definition = draft.get("definition") or {}
    programs, workflows = _known_catalogs()
    canonical, issues = validate_overlay_payload(
        definition,
        known_program_types=programs,
        known_workflow_types=workflows,
    )
    return {
        "valid": not any(issue.get("severity") == "error" for issue in issues),
        "issues": issues,
        "normalized": canonical,
    }


def activate_routing_definition_draft_admin(
    runtime: Any,
    auth: AuthContext,
    draft_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    expected_revision = str(body.get("expected_revision") or "")
    if not expected_revision:
        raise ValueError("expected_revision is required")
    programs, workflows = _known_catalogs()
    draft = routing_definition_store().get_draft(draft_id)
    canonical, issues = validate_overlay_payload(
        draft.get("definition") or {},
        known_program_types=programs,
        known_workflow_types=workflows,
    )
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        raise ValueError(
            "; ".join(issue["message"] for issue in errors) or "routing validation failed"
        )
    activated = routing_definition_store().activate(
        draft_id,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
    )
    reload_ok = reload_routing_into_runtime(runtime)
    result = {"activated": activated, "issues": issues, "reload_ok": reload_ok}
    if reload_ok is False:
        result["warning"] = (
            "routing activated on disk but runtime reload failed; last good merge kept"
        )
    return result


def preview_routing_from_draft(
    draft_id: str,
) -> tuple[list[Any], list[dict[str, str]], dict[str, Any] | None]:
    draft = routing_definition_store().get_draft(draft_id)
    programs, workflows = _known_catalogs()
    canonical, issues = validate_overlay_payload(
        draft.get("definition") or {},
        known_program_types=programs,
        known_workflow_types=workflows,
    )
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        return [], issues, None
    return overlay_intents_from_payload(canonical), issues, canonical
