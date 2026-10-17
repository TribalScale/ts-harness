"""Admin BFF helpers for managed program definitions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ts_harness.gateway.auth import AuthContext
from ts_harness.repo_paths import ensure_scripts_importable, programs_dir
from ts_harness.workflows.definition_validation import ValidationIssue
from ts_harness.workflows.materialize import build_workflow_catalog
from ts_harness.workflows.program_adapters import build_default_adapter_registry
from ts_harness.workflows.program_definition_store import ProgramDefinitionStore
from ts_harness.workflows.program_definition_validation import (
    validate_program_definition,
)

ensure_scripts_importable()

import program_definitions  # noqa: E402


class ProgramDefinitionValidationError(ValueError):
    """Raised when a draft cannot be activated due to semantic errors."""


def program_definition_store_root() -> Path:
    return Path(
        os.environ.get(
            "TS_HARNESS_PROGRAM_DEFINITION_STORE",
            "program-definitions",
        )
    )


def _read_definition(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"program definition must be an object: {path.name}")
    return data


def _builtin_templates() -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    root = programs_dir()
    if not root.exists():
        return templates
    for path in sorted(root.glob("*.json")):
        if path.name in {"schema.json", "manifest.json"}:
            continue
        payload = _read_definition(path)
        loaded = program_definitions.load_program_payload(payload)
        templates.append(
            {
                "program_type": loaded.program_type,
                "definition_version": loaded.definition_version,
                "display_name": loaded.display_name or loaded.program_type,
            }
        )
    return templates


def built_in_program_types() -> set[str]:
    return {str(item["program_type"]) for item in _builtin_templates()}


def program_definition_store() -> ProgramDefinitionStore:
    return ProgramDefinitionStore(
        program_definition_store_root(),
        built_in_types=built_in_program_types(),
    )


def _catalogs() -> dict[str, Any]:
    return {
        "workflow_catalog": build_workflow_catalog(),
        "adapter_names": set(build_default_adapter_registry().names()),
        "built_in_types": built_in_program_types(),
    }


def _issue_payload(issue: ValidationIssue) -> dict[str, str]:
    return {
        "severity": issue.severity,
        "code": issue.code,
        "path": issue.path,
        "message": issue.message,
    }


def _require_body_object(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _optional_editor_metadata(body: dict[str, Any]) -> dict[str, Any] | None:
    if "editor_metadata" not in body:
        return None
    metadata = body["editor_metadata"]
    if not isinstance(metadata, dict):
        raise ValueError("editor_metadata must be an object")
    return metadata


def _format_validation_error(issues: list[ValidationIssue]) -> str:
    details = [
        f"{issue.code} at {issue.path}: {issue.message}"
        for issue in issues
        if issue.severity == "error"
    ]
    return "program definition validation failed: " + "; ".join(details)


def list_program_definitions_admin(runtime: Any) -> dict[str, Any]:
    del runtime  # auth/runtime already enforced by the route
    definitions: list[dict[str, Any]] = [
        {
            **template,
            "status": "built_in",
            "read_only": True,
        }
        for template in _builtin_templates()
    ]
    store = program_definition_store()
    for item in store.list_definitions():
        definitions.append({**item, "read_only": False})
    return {"definitions": definitions, "total": len(definitions)}


def program_definition_catalog_admin(runtime: Any) -> dict[str, Any]:
    del runtime
    workflows = []
    for entry in build_workflow_catalog().values():
        workflows.append(
            {
                "workflow_type": entry["workflow_type"],
                "status": entry.get("status"),
                "available_versions": list(entry.get("available_versions") or []),
                "active_version": entry.get("active_version"),
                "resolvable": bool(entry.get("resolvable")),
            }
        )
    adapters = [
        {"id": name, "label": name}
        for name in build_default_adapter_registry().names()
    ]
    return {
        "workflows": sorted(workflows, key=lambda item: item["workflow_type"]),
        "adapters": adapters,
        "transition_modes": sorted(program_definitions.VALID_TRANSITION_MODES),
        "templates": _builtin_templates(),
    }


def get_program_definition_admin(program_type: str) -> dict[str, Any]:
    store = program_definition_store()
    active = store.get_active(program_type)
    if active is not None:
        return {"status": "active", "read_only": False, "definition": active}

    for path in sorted(programs_dir().glob("*.json")):
        if path.name in {"schema.json", "manifest.json"}:
            continue
        payload = _read_definition(path)
        if str(payload.get("program_type") or path.stem) == program_type:
            return {
                "status": "built_in",
                "read_only": True,
                "definition": payload,
            }
    raise FileNotFoundError(f"program definition not found: {program_type}")


def create_program_definition_draft_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    del runtime
    body = _require_body_object(body)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    draft = program_definition_store().create_draft(
        definition,
        actor_id=auth.actor_id,
        editor_metadata=_optional_editor_metadata(body),
    )
    return {"draft": draft}


def get_program_definition_draft_admin(draft_id: str) -> dict[str, Any]:
    return {"draft": program_definition_store().get_draft(draft_id)}


def update_program_definition_draft_admin(
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
    draft = program_definition_store().update_draft(
        draft_id,
        definition,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
        **update_kwargs,
    )
    return {"draft": draft}


def delete_program_definition_draft_admin(draft_id: str) -> dict[str, Any]:
    program_definition_store().delete_draft(draft_id)
    return {"deleted": draft_id}


def program_action_permission(program_type: str, node_id: str | None = None) -> str:
    """Permission a human needs to enter ``node_id`` of ``program_type``."""
    ensure_scripts_importable()
    import program_definitions

    text = str(program_type or "").strip()
    node = str(node_id or "").strip() or None
    if not text:
        return program_definitions.DEFAULT_PROGRAM_PERMISSION
    try:
        payload = _resolve_program_definition(text)
        definition = program_definitions.load_program_payload(payload)
    except Exception:
        return program_definitions.DEFAULT_PROGRAM_PERMISSION
    return program_definitions.required_permission_for(definition, node)


def validate_program_definition_draft_admin(
    runtime: Any,
    draft_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = body or {}
    override = body.get("definition")
    if override is not None and not isinstance(override, dict):
        raise ValueError("definition must be an object")
    definition = (
        override
        or program_definition_store().get_draft(draft_id)["definition"]
    )
    loaded, issues = validate_program_definition(
        definition,
        config=getattr(runtime, "config", None),
        **_catalogs(),
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    normalized = (
        program_definitions.program_to_payload(loaded)
        if loaded is not None
        else None
    )
    return {
        "valid": not errors,
        "issues": [_issue_payload(issue) for issue in issues],
        "normalized_definition": normalized,
    }


def activate_program_definition_draft_admin(
    runtime: Any,
    auth: AuthContext,
    draft_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    expected_revision = str(body.get("expected_revision") or "")
    if not expected_revision:
        raise ValueError("expected_revision is required")
    store = program_definition_store()
    draft = store.get_draft(draft_id)
    _loaded, issues = validate_program_definition(
        draft["definition"],
        config=getattr(runtime, "config", None),
        **_catalogs(),
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ProgramDefinitionValidationError(_format_validation_error(errors))
    active = store.activate(
        draft_id,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
    )
    return {"active": active}


def _resolve_program_definition(program_type: str) -> dict[str, Any]:
    """Return active or built-in definition payload for program_type."""
    store = program_definition_store()
    active = store.get_active(program_type)
    if active is not None:
        return active
    for path in sorted(programs_dir().glob("*.json")):
        if path.name in {"schema.json", "manifest.json"}:
            continue
        payload = _read_definition(path)
        if str(payload.get("program_type") or path.stem) == program_type:
            return payload
    raise FileNotFoundError(f"program definition not found: {program_type}")


def start_program_definition_admin(
    runtime: Any,
    auth: AuthContext,
    program_type: str,
    body: dict[str, Any],
    service: Any,
) -> dict[str, Any]:
    """Start a program instance for a known program definition.

    DeliveryProgramService pins the active-or-built-in definition snapshot and
    resolved workflow versions before materializing the entry workflow.
    """
    body = _require_body_object(body)
    definition = _resolve_program_definition(program_type)
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary is required")
    startable = list(definition.get("startable_nodes") or [])
    entry_phase = str(
        body.get("entry_phase")
        or body.get("entry_node")
        or (startable[0] if startable else "")
        or "prd"
    ).strip()
    if not entry_phase:
        raise ValueError("entry_phase is required")
    artifacts = body.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be an object")
    extra_input = body.get("input") or {}
    if not isinstance(extra_input, dict):
        raise ValueError("input must be an object")

    from ts_harness.policy.permissions import department_for_email

    result = service.create_program(
        entry_node=entry_phase,
        program_type=program_type,
        summary=summary,
        tenant_id=auth.tenant_id,
        binding_key=str(body.get("binding_key") or "web:program-start"),
        requester_actor_id=auth.actor_id,
        artifacts=artifacts,
        extra_input=extra_input,
        actor=str(auth.actor_id or "admin"),
        department=department_for_email(getattr(runtime, "config", None), getattr(auth, "email", None)),
    )
    payload = {
        "status": "started",
        "program_type": program_type,
        "program_id": getattr(result, "program_id", None),
        "workflow_id": getattr(result, "workflow_id", None),
        "workflow_type": getattr(result, "workflow_type", None),
        "entry_phase": getattr(result, "entry_phase", entry_phase),
        "task_ids": list(getattr(result, "task_ids", []) or []),
        "first_step": getattr(result, "first_step", None),
        "session_id": None,
    }
    if runtime is not None and hasattr(runtime, "sessions"):
        try:
            session = runtime.sessions.get_or_create(
                tenant_id=auth.tenant_id,
                source="web",
                agent_id="orchestrator",
                channel="web",
                thread_id=f"program-start-{payload['program_id']}",
            )
            payload["session_id"] = getattr(session, "session_id", None)
        except Exception:
            pass
    return payload
