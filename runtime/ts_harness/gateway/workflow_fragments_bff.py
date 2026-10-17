"""Admin BFF helpers for managed workflow stage fragments."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.workflow_definitions_bff import (
    _catalogs,
    _issue_payload,
    _optional_editor_metadata,
    _require_body_object,
)
from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.definition_validation import (
    ValidationIssue,
    validate_fragment_definition,
)
from ts_harness.workflows.fragment_definition_store import FragmentDefinitionStore

ensure_scripts_importable()

import workflow_fragments  # noqa: E402


class FragmentValidationError(ValueError):
    """Raised when a fragment draft cannot be activated due to semantic errors."""


def workflow_fragment_store_root() -> Path:
    return Path(
        os.environ.get(
            "TS_HARNESS_FRAGMENT_DEFINITION_STORE",
            "workflow-definitions/fragments",
        )
    )


def _builtin_fragments() -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    root = workflow_fragments.builtin_fragments_dir()
    for fragment_id in workflow_fragments.list_builtin_fragment_ids():
        payload = json.loads((root / f"{fragment_id}.json").read_text(encoding="utf-8"))
        fragment = workflow_fragments.load_fragment_payload(payload)
        fragments.append(
            {
                "fragment_id": fragment.fragment_id,
                "definition_version": fragment.definition_version,
                "display_name": fragment.display_name or fragment.fragment_id,
                "payload": payload,
            }
        )
    return fragments


def built_in_fragment_ids() -> set[str]:
    return set(workflow_fragments.list_builtin_fragment_ids())


def workflow_fragment_store() -> FragmentDefinitionStore:
    return FragmentDefinitionStore(
        workflow_fragment_store_root(),
        built_in_types=built_in_fragment_ids(),
    )


def _fragment_catalogs(runtime: Any) -> dict[str, set[str]]:
    catalogs = _catalogs(runtime)
    return {
        "hook_names": catalogs["hook_names"],
        "agent_ids": catalogs["agent_ids"],
        "skill_ids": catalogs["skill_ids"],
    }


def _format_validation_error(issues: list[ValidationIssue]) -> str:
    details = [
        f"{issue.code} at {issue.path}: {issue.message}"
        for issue in issues
        if issue.severity == "error"
    ]
    return "fragment validation failed: " + "; ".join(details)


def _summary(fragment: workflow_fragments.Fragment) -> dict[str, Any]:
    return {
        "params": fragment.params,
        "inputs": fragment.inputs,
        "stage_names": [str(stage.get("name")) for stage in fragment.stages],
    }


def list_workflow_fragments_admin() -> dict[str, Any]:
    fragments: list[dict[str, Any]] = []
    for item in _builtin_fragments():
        fragment = workflow_fragments.load_fragment_payload(item["payload"])
        fragments.append(
            {
                "fragment_id": item["fragment_id"],
                "definition_version": item["definition_version"],
                "display_name": item["display_name"],
                "status": "built_in",
                "read_only": True,
                **_summary(fragment),
            }
        )
    store = workflow_fragment_store()
    for item in store.list_definitions():
        entry = {**item, "read_only": False}
        if item.get("status") == "active":
            active = store.get_active(str(item["fragment_id"]))
            if active is not None:
                fragment = workflow_fragments.load_fragment_payload(active)
                entry.update(
                    {"display_name": fragment.display_name or fragment.fragment_id, **_summary(fragment)}
                )
        fragments.append(entry)
    return {"fragments": fragments, "total": len(fragments)}


def get_workflow_fragment_admin(fragment_id: str, version: str = "") -> dict[str, Any]:
    version = str(version or "").strip()
    if version:
        try:
            fragment = _resolve_with_store(fragment_id, version)
        except workflow_fragments.FragmentError as exc:
            raise FileNotFoundError(str(exc)) from exc
        builtin = fragment_id in built_in_fragment_ids()
        return {
            "status": "built_in" if builtin else "active",
            "read_only": builtin,
            "definition": workflow_fragments.fragment_to_payload(fragment),
        }
    active = workflow_fragment_store().get_active(fragment_id)
    if active is not None:
        return {"status": "active", "read_only": False, "definition": active}
    for item in _builtin_fragments():
        if item["fragment_id"] == fragment_id:
            return {"status": "built_in", "read_only": True, "definition": item["payload"]}
    raise FileNotFoundError(f"workflow fragment not found: {fragment_id}")


def _resolve_with_store(fragment_id: str, version: str) -> workflow_fragments.Fragment:
    return workflow_fragments.resolve_fragment(
        fragment_id,
        version,
        store_root=workflow_fragment_store_root(),
    )


def create_workflow_fragment_draft_admin(auth: AuthContext, body: dict[str, Any]) -> dict[str, Any]:
    body = _require_body_object(body)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("definition is required")
    draft = workflow_fragment_store().create_draft(
        definition,
        actor_id=auth.actor_id,
        editor_metadata=_optional_editor_metadata(body),
    )
    return {"draft": draft}


def get_workflow_fragment_draft_admin(draft_id: str) -> dict[str, Any]:
    return {"draft": workflow_fragment_store().get_draft(draft_id)}


def update_workflow_fragment_draft_admin(
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
    draft = workflow_fragment_store().update_draft(
        draft_id,
        definition,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
        **update_kwargs,
    )
    return {"draft": draft}


def delete_workflow_fragment_draft_admin(draft_id: str) -> dict[str, Any]:
    workflow_fragment_store().delete_draft(draft_id)
    return {"deleted": draft_id}


def validate_workflow_fragment_draft_admin(
    runtime: Any,
    draft_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = body or {}
    override = body.get("definition")
    if override is not None and not isinstance(override, dict):
        raise ValueError("definition must be an object")
    definition = override or workflow_fragment_store().get_draft(draft_id)["definition"]
    fragment, issues = validate_fragment_definition(definition, **_fragment_catalogs(runtime))
    errors = [issue for issue in issues if issue.severity == "error"]
    return {
        "valid": not errors,
        "issues": [_issue_payload(issue) for issue in issues],
        "normalized_definition": (
            workflow_fragments.fragment_to_payload(fragment) if fragment is not None else None
        ),
    }


def activate_workflow_fragment_draft_admin(
    runtime: Any,
    auth: AuthContext,
    draft_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    body = _require_body_object(body)
    expected_revision = str(body.get("expected_revision") or "")
    if not expected_revision:
        raise ValueError("expected_revision is required")
    store = workflow_fragment_store()
    draft = store.get_draft(draft_id)
    fragment_id = str(draft.get("fragment_id") or "")
    if fragment_id in built_in_fragment_ids():
        raise ValueError(f"cannot activate over built-in fragment: {fragment_id}")
    _fragment, issues = validate_fragment_definition(draft["definition"], **_fragment_catalogs(runtime))
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise FragmentValidationError(_format_validation_error(errors))
    active = store.activate(
        draft_id,
        expected_revision=expected_revision,
        actor_id=auth.actor_id,
    )
    return {"active": active}
