"""Admin BFF helpers for managed workflow definitions."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.cron_store import (
    validate_skill_hints_against_catalog,
)
from ts_harness.repo_paths import ensure_scripts_importable, workflows_dir
from ts_harness.workflows.definition_store import WorkflowDefinitionStore
from ts_harness.workflows.definition_validation import (
    ValidationIssue,
    validate_workflow_definition,
)
from ts_harness.workflows.hooks import build_default_registry
from ts_harness.workflows.materialize import build_fork_graph, build_workflow_catalog

ensure_scripts_importable()

import workflow_definitions  # noqa: E402


class WorkflowDefinitionValidationError(ValueError):
    """Raised when a draft cannot be activated due to semantic errors."""


def workflow_definition_store_root() -> Path:
    return Path(
        os.environ.get(
            "TS_HARNESS_WORKFLOW_DEFINITION_STORE",
            "workflow-definitions",
        )
    )


def _read_definition(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"workflow definition must be an object: {path.name}")
    return data


def _builtin_templates() -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    root = workflows_dir()
    if not root.exists():
        return templates
    for path in sorted(root.glob("*.json")):
        if path.name in {"schema.json", "manifest.json"}:
            continue
        payload = _read_definition(path)
        skeleton = workflow_definitions.load_skeleton_payload(payload)
        templates.append(
            {
                "workflow_type": skeleton.workflow_type,
                "definition_version": skeleton.definition_version,
                "display_name": (
                    skeleton.display_name
                    or skeleton.workflow_type
                ),
            }
        )
    return templates


def built_in_workflow_types() -> set[str]:
    return {str(item["workflow_type"]) for item in _builtin_templates()}


def workflow_definition_store() -> WorkflowDefinitionStore:
    return WorkflowDefinitionStore(
        workflow_definition_store_root(),
        built_in_types=built_in_workflow_types(),
    )


def _skill_is_injectable(skill_id: str, meta: Any) -> bool:
    try:
        catalog = type(
            "_Catalog",
            (),
            {"index": {"x": meta}},
        )()
        validate_skill_hints_against_catalog([skill_id], catalog)
    except ValueError:
        return False
    if getattr(meta, "category", None) == "drafts":
        return False
    return True


def _catalogs(runtime: Any) -> dict[str, set[str]]:
    hook_names = set(build_default_registry().names())
    agent_ids = {
        str(getattr(agent, "id", ""))
        for agent in getattr(runtime.config, "agents", [])
    }
    agent_ids.discard("")
    skill_ids: set[str] = set()
    catalog = getattr(runtime, "skill_catalog", None)
    for meta in (getattr(catalog, "index", {}) or {}).values():
        skill_id = str(getattr(meta, "id", "") or "")
        if skill_id and _skill_is_injectable(skill_id, meta):
            skill_ids.add(skill_id)
    return {
        "hook_names": hook_names,
        "agent_ids": agent_ids,
        "skill_ids": skill_ids,
        "built_in_types": built_in_workflow_types(),
    }


def _agent_tools(runtime: Any) -> dict[str, set[str]]:
    from ts_harness.tools.toolsets import agent_effective_tools

    tools: dict[str, set[str]] = {}
    for agent in getattr(runtime.config, "agents", []):
        agent_id = str(getattr(agent, "id", "") or "")
        if not agent_id:
            continue
        granted = set(agent_effective_tools(agent, runtime.config))
        tools[agent_id] = granted - set(getattr(agent, "tool_deny", None) or [])
    return tools


def _publish_tools(runtime: Any, agent_tools: dict[str, set[str]] | None = None) -> set[str]:
    from ts_harness.tools.mcp_registry import registered_mcp_tool_names
    from ts_harness.tools.policy import publish_tool_names

    tools = agent_tools if agent_tools is not None else _agent_tools(runtime)
    candidates = set(registered_mcp_tool_names())
    for granted in tools.values():
        candidates |= set(granted)
    return publish_tool_names(candidates, runtime.config)


def workflow_validation_kwargs(runtime: Any) -> dict[str, Any]:
    """Catalog keyword arguments for ``validate_workflow_definition``."""
    agent_tools = _agent_tools(runtime)
    return {
        **_catalogs(runtime),
        **_child_workflow_catalogs(),
        "agent_tools": agent_tools,
        "publish_tools": _publish_tools(runtime, agent_tools),
    }


def _child_workflow_catalogs() -> dict[str, Any]:
    return {"workflow_catalog": build_workflow_catalog(), "fork_graph": build_fork_graph()}


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
    return "workflow definition validation failed: " + "; ".join(details)


def list_workflow_definitions_admin(runtime: Any) -> dict[str, Any]:
    definitions: list[dict[str, Any]] = [
        {
            **template,
            "status": "built_in",
            "read_only": True,
        }
        for template in _builtin_templates()
    ]
    store = workflow_definition_store()
    for item in store.list_definitions():
        definitions.append({**item, "read_only": False})
    return {"definitions": definitions, "total": len(definitions)}


def workflow_definition_catalog_admin(runtime: Any) -> dict[str, Any]:
    catalog = getattr(runtime, "skill_catalog", None)
    skills = []
    for meta in (getattr(catalog, "index", {}) or {}).values():
        skill_id = str(getattr(meta, "id", "") or "")
        if skill_id and _skill_is_injectable(skill_id, meta):
            skills.append({"id": skill_id, "label": skill_id})

    agents = []
    for agent in getattr(runtime.config, "agents", []):
        agent_id = str(getattr(agent, "id", "") or "")
        if agent_id:
            agents.append(
                {
                    "id": agent_id,
                    "label": str(
                        getattr(agent, "label", None)
                        or getattr(agent, "display_name", None)
                        or getattr(agent, "name", None)
                        or agent_id
                    ),
                }
            )

    return {
        "hooks": build_default_registry().names(),
        "agents": agents,
        "agent_tools": {
            agent_id: sorted(names) for agent_id, names in _agent_tools(runtime).items()
        },
        "skills": sorted(skills, key=lambda item: item["id"]),
        "stage_kinds": ["task", "expand", "compensate"],
        "semantic_validators": sorted(workflow_definitions.KNOWN_SEMANTIC_VALIDATORS),
        "present_prompts": ["approval", "inform", "intake", "merge", "none"],
        "notification_events": [
            "completed",
            "blocked",
            "failed",
            "awaiting_approval",
            "awaiting_external",
        ],
        "templates": _builtin_templates(),
        "child_workflows": [
            {
                "workflow_type": entry["workflow_type"],
                "active_version": entry.get("active_version"),
                "resolvable": bool(entry.get("resolvable")),
                "workflow_input_keys": list(entry.get("workflow_input_keys") or []),
            }
            for entry in sorted(build_workflow_catalog().values(), key=lambda item: item["workflow_type"])
            if entry.get("resolvable")
        ],
    }


def get_workflow_definition_admin(workflow_type: str) -> dict[str, Any]:
    store = workflow_definition_store()
    active = store.get_active(workflow_type)
    if active is not None:
        return {"status": "active", "read_only": False, "definition": active}

    for path in sorted(workflows_dir().glob("*.json")):
        if path.name in {"schema.json", "manifest.json"}:
            continue
        payload = _read_definition(path)
        if str(payload.get("workflow_type") or path.stem) == workflow_type:
            return {
                "status": "built_in",
                "read_only": True,
                "definition": payload,
            }
    raise FileNotFoundError(f"workflow definition not found: {workflow_type}")


def create_workflow_definition_draft_admin(
    runtime: Any,
    auth: AuthContext,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    draft = workflow_definition_store().create_draft(
        definition,
        actor_id=auth.actor_id,
        editor_metadata=_optional_editor_metadata(body),
    )
    return {"draft": draft}


def get_workflow_definition_draft_admin(draft_id: str) -> dict[str, Any]:
    return {"draft": workflow_definition_store().get_draft(draft_id)}


def update_workflow_definition_draft_admin(
    runtime: Any,
    auth: AuthContext,
    draft_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
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
    draft = workflow_definition_store().update_draft(
        draft_id,
        definition,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
        **update_kwargs,
    )
    return {"draft": draft}


def delete_workflow_definition_draft_admin(draft_id: str) -> dict[str, Any]:
    workflow_definition_store().delete_draft(draft_id)
    return {"deleted": draft_id}


def validate_workflow_definition_draft_admin(
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
        or workflow_definition_store().get_draft(draft_id)["definition"]
    )
    skeleton, issues = validate_workflow_definition(
        definition,
        **workflow_validation_kwargs(runtime),
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    normalized = (
        workflow_definitions.skeleton_to_payload(skeleton)
        if skeleton is not None
        else None
    )
    return {
        "valid": not errors,
        "issues": [_issue_payload(issue) for issue in issues],
        "normalized_definition": normalized,
    }


def activate_workflow_definition_draft_admin(
    runtime: Any,
    auth: AuthContext,
    draft_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    expected_revision = str(body.get("expected_revision") or "")
    if not expected_revision:
        raise ValueError("expected_revision is required")
    store = workflow_definition_store()
    draft = store.get_draft(draft_id)
    _skeleton, issues = validate_workflow_definition(
        draft["definition"],
        **workflow_validation_kwargs(runtime),
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise WorkflowDefinitionValidationError(_format_validation_error(errors))
    active = store.activate(
        draft_id,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
    )
    return {"active": active}


def start_workflow_definition_admin(
    runtime: Any,
    auth: AuthContext,
    workflow_type: str,
    body: dict[str, Any],
    engine: Any,
) -> dict[str, Any]:
    body = _require_body_object(body)
    active = workflow_definition_store().get_active(workflow_type)
    if active is None:
        raise FileNotFoundError(
            f"active managed workflow not found: {workflow_type}"
        )
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary is required")
    extra_input = body.get("input") or {}
    if not isinstance(extra_input, dict):
        raise ValueError("input must be an object")
    thread_id = f"workflow-start-{uuid.uuid4()}"
    session = runtime.sessions.get_or_create(
        tenant_id=auth.tenant_id,
        source="web",
        agent_id="orchestrator",
        channel="web",
        thread_id=thread_id,
    )
    result = engine.start_promotion(
        session=session,
        workflow_type=workflow_type,
        summary=summary,
        tenant_id=auth.tenant_id,
        binding_key=f"web:{thread_id}",
        requester_actor_id=auth.actor_id,
        correlation_id=str(body.get("correlation_id") or ""),
        transcript_messages=[],
        extra_input=extra_input,
    )
    return {
        "status": "started",
        "workflow_type": workflow_type,
        "session_id": session.session_id,
        **result,
    }
