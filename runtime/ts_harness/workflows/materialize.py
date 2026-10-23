"""Materialize workflow skeleton JSON into memory-api task payloads."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable, workflows_dir
from ts_harness.workflows.definition_paths import (
    definition_content_revision,
    history_dir,
    history_filename,
    history_filename_prefixes,
    safe_component,
)

ensure_scripts_importable()

import workflow_definitions  # noqa: E402

logger = logging.getLogger(__name__)


def managed_workflows_dir() -> Path | None:
    configured = os.environ.get(
        "TS_HARNESS_MANAGED_WORKFLOWS_DIR",
        "",
    ).strip()
    if not configured:
        return None
    return Path(configured)


def definition_store_root() -> Path | None:
    configured = os.environ.get(
        "TS_HARNESS_WORKFLOW_DEFINITION_STORE",
        "",
    ).strip()
    if not configured:
        return None
    return Path(configured)


def _manifest_payload() -> dict[str, Any]:
    store_root = definition_store_root()
    if store_root is None:
        return {}
    path = store_root / "active" / "manifest.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("workflow definition manifest must be an object")
    return data


def _manifest_history_path(workflow_type: str) -> Path | None:
    store_root = definition_store_root()
    if store_root is None:
        return None
    manifest = _manifest_payload()
    meta = dict((manifest.get("workflows") or {}).get(workflow_type) or {})
    version = str(meta.get("definition_version") or "")
    revision = str(meta.get("revision") or "")
    if not version or not revision:
        return None
    primary = history_dir(store_root, workflow_type) / history_filename(
        version,
        revision,
    )
    if primary.exists():
        return primary
    for prefix in history_filename_prefixes(version)[1:]:
        legacy = (
            history_dir(store_root, workflow_type)
            / f"{prefix}{revision}.json"
        )
        if legacy.exists():
            return legacy
    return None


def _manifest_entry(workflow_type: str) -> dict[str, Any] | None:
    manifest = _manifest_payload()
    workflows = dict(manifest.get("workflows") or {})
    meta = workflows.get(workflow_type)
    if not isinstance(meta, dict):
        return None
    return meta


def _active_matches_manifest(workflow_type: str, path: Path) -> bool:
    meta = _manifest_entry(workflow_type)
    if meta is None:
        return True
    revision = str(meta.get("revision") or "").strip()
    version = str(meta.get("definition_version") or "").strip()
    if not revision or not version:
        # Partial/legacy manifest entries are not trustworthy.
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return (
        str(payload.get("definition_version") or "") == version
        and definition_content_revision(payload) == revision
    )


def resolve_skeleton_path(
    workflow_type: str,
    skeleton_dir: Path | None = None,
) -> Path:
    managed_root = managed_workflows_dir()
    if managed_root is not None:
        managed_path = managed_root / f"{safe_component(workflow_type, field='workflow_type')}.json"
        in_manifest = _manifest_entry(workflow_type) is not None
        if managed_path.exists() and _active_matches_manifest(
            workflow_type,
            managed_path,
        ):
            return managed_path
        history_path = _manifest_history_path(workflow_type)
        if history_path is not None:
            return history_path
        if in_manifest or managed_path.exists():
            raise FileNotFoundError(
                "managed workflow definition unavailable for "
                f"{workflow_type}: active/history mismatch"
            )
    root = skeleton_dir or workflows_dir()
    path = root / f"{workflow_type}.json"
    if not path.exists():
        raise FileNotFoundError(f"workflow skeleton not found: {path}")
    return path


def load_skeleton(
    workflow_type: str,
    skeleton_dir: Path | None = None,
) -> workflow_definitions.WorkflowSkeleton:
    return workflow_definitions.load_skeleton(
        resolve_skeleton_path(workflow_type, skeleton_dir)
    )


def list_skeleton_types(skeleton_dir: Path | None = None) -> list[str]:
    root = skeleton_dir or workflows_dir()
    names = {
        path.stem
        for path in root.glob("*.json")
        if path.name not in {"schema.json", "manifest.json"}
    }
    managed_root = managed_workflows_dir()
    if managed_root is not None and managed_root.exists():
        names.update(
            path.stem
            for path in managed_root.glob("*.json")
            if path.name != "manifest.json"
        )
    manifest = _manifest_payload()
    names.update(
        safe_component(str(name), field="workflow_type")
        for name in (manifest.get("workflows") or {})
    )
    store_root = definition_store_root()
    if store_root is not None:
        history_root = store_root / "history"
        if history_root.exists():
            names.update(
                path.name
                for path in history_root.iterdir()
                if path.is_dir()
            )
    return sorted(names)


def _schema_property_keys(schema: Any) -> set[str]:
    keys: set[str] = set()
    if not isinstance(schema, dict):
        return keys
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, subschema in properties.items():
            keys.add(str(key))
            keys.update(_schema_property_keys(subschema))
    for combo in ("anyOf", "oneOf", "allOf"):
        for subschema in list(schema.get(combo) or []):
            keys.update(_schema_property_keys(subschema))
    items = schema.get("items")
    if isinstance(items, dict):
        keys.update(_schema_property_keys(items))
    return keys


def contract_output_keys(payload: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    contracts = payload.get("contracts")
    if not isinstance(contracts, dict):
        return keys
    for contract in contracts.values():
        if not isinstance(contract, dict):
            continue
        keys.update(_schema_property_keys(contract.get("output_schema")))
    return keys


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _history_versions(workflow_type: str) -> list[str]:
    store_root = definition_store_root()
    if store_root is None:
        return []
    root = history_dir(store_root, workflow_type)
    if not root.exists():
        return []
    versions: set[str] = set()
    for path in root.glob("*.json"):
        payload = _read_json_object(path)
        if payload is None:
            continue
        version = str(payload.get("definition_version") or "").strip()
        if version:
            versions.add(version)
    return sorted(versions)


def workflow_definition_resolvable(
    workflow_type: str,
    definition_version: str | None = None,
    *,
    skeleton_dir: Path | None = None,
) -> bool:
    """Return True when a workflow type/version can be loaded fail-closed."""
    requested = str(definition_version or "").strip()
    try:
        path = resolve_skeleton_path(workflow_type, skeleton_dir)
        payload = _read_json_object(path)
        if payload is None:
            return False
        if not requested:
            return True
        return str(payload.get("definition_version") or "") == requested
    except (OSError, ValueError, FileNotFoundError):
        pass

    if not requested:
        return False

    store_root = definition_store_root()
    if store_root is None:
        return False
    from ts_harness.workflows.definition_paths import (
        list_history_paths_for_version,
    )

    matches = list_history_paths_for_version(
        store_root,
        workflow_type,
        requested,
    )
    for path in matches:
        payload = _read_json_object(path)
        if payload is None:
            continue
        if (
            str(payload.get("workflow_type") or path.parent.name) == workflow_type
            and str(payload.get("definition_version") or "") == requested
        ):
            return True
    return False


def build_workflow_catalog(
    skeleton_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a fail-closed workflow catalog for program validation.

    Each entry includes exact available versions, status, resolvability, and
    statically discoverable contract output keys.
    """
    root = skeleton_dir or workflows_dir()
    catalog: dict[str, dict[str, Any]] = {}

    for workflow_type in list_skeleton_types(root):
        versions: set[str] = set()
        status = "unavailable"
        active_version: str | None = None
        contract_keys: set[str] = set()
        payload: dict[str, Any] | None = None

        builtin_path = root / f"{workflow_type}.json"
        if builtin_path.exists():
            payload = _read_json_object(builtin_path)
            if payload is not None:
                version = str(payload.get("definition_version") or "").strip()
                if version:
                    versions.add(version)
                    active_version = version
                contract_keys.update(contract_output_keys(payload))
                status = "built_in"

        managed_root = managed_workflows_dir()
        managed_path = None
        if managed_root is not None:
            managed_path = managed_root / (
                f"{safe_component(workflow_type, field='workflow_type')}.json"
            )
            if managed_path.exists():
                managed_payload = _read_json_object(managed_path)
                if managed_payload is not None:
                    version = str(
                        managed_payload.get("definition_version") or ""
                    ).strip()
                    if version:
                        versions.add(version)
                        active_version = version
                    contract_keys.update(contract_output_keys(managed_payload))
                    status = "active"
                    payload = managed_payload

        meta = _manifest_entry(workflow_type)
        if meta is not None:
            status = "active"
            version = str(meta.get("definition_version") or "").strip()
            if version:
                versions.add(version)
                active_version = version

        for version in _history_versions(workflow_type):
            versions.add(version)

        resolvable = workflow_definition_resolvable(
            workflow_type,
            active_version,
            skeleton_dir=root,
        )
        if not resolvable and versions:
            resolvable = any(
                workflow_definition_resolvable(
                    workflow_type,
                    version,
                    skeleton_dir=root,
                )
                for version in sorted(versions)
            )

        if payload is None:
            try:
                resolved = resolve_skeleton_path(workflow_type, root)
                payload = _read_json_object(resolved)
                if payload is not None:
                    contract_keys.update(contract_output_keys(payload))
            except (OSError, ValueError, FileNotFoundError):
                pass

        catalog[workflow_type] = {
            "workflow_type": workflow_type,
            "status": status,
            "available_versions": sorted(versions),
            "active_version": active_version,
            "resolvable": resolvable,
            "contract_output_keys": sorted(contract_keys),
            "workflow_input_keys": sorted(_workflow_input_keys(workflow_type, root)),
        }
    return catalog


_WORKFLOW_TOKEN_PREFIX = "$workflow."


def _workflow_input_keys(workflow_type: str, skeleton_dir: Path | None = None) -> set[str]:
    """``$workflow.<key>`` references in the current definition's input maps."""
    try:
        skeleton = load_skeleton(workflow_type, skeleton_dir)
    except Exception:
        return set()
    keys: set[str] = set()
    for stage in skeleton.stages:
        for expr in (stage.input_map or {}).values():
            token = str(expr or "").strip()
            if token.startswith(_WORKFLOW_TOKEN_PREFIX):
                keys.add(token[len(_WORKFLOW_TOKEN_PREFIX) :])
    return keys


def build_fork_graph(skeleton_dir: Path | None = None) -> dict[str, set[str]]:
    """Child workflow types each current definition forks, for cycle checks."""
    graph: dict[str, set[str]] = {}
    for workflow_type in list_skeleton_types(skeleton_dir):
        try:
            skeleton = load_skeleton(workflow_type, skeleton_dir)
        except Exception:
            continue
        children = {fork_child_type(stage.barrier) for stage in skeleton.stages}
        children.discard("")
        if children:
            graph[workflow_type] = children
    return graph


FORK_HOOKS = frozenset({"fork_children", "fork_work_order_wave"})
DEFAULT_WAVE_CHILD_TYPE = "deliver_work_order"


def fork_child_type(barrier: dict[str, Any] | None) -> str:
    """Child workflow type a fork barrier starts, or ``""`` when it is not a fork."""
    config = dict(barrier or {})
    hook = str(config.get("hook") or "")
    if hook not in FORK_HOOKS:
        return ""
    child_type = str(config.get("workflow_type") or "").strip()
    if not child_type and hook == "fork_work_order_wave":
        return DEFAULT_WAVE_CHILD_TYPE
    return child_type


def _child_version(stage: Any) -> str:
    """Current version of the child a fork stage starts, pinned when the parent starts."""
    child_type = fork_child_type(getattr(stage, "barrier", None))
    if not child_type:
        return ""
    try:
        return str(load_skeleton(child_type).definition_version or "")
    except Exception:
        logger.warning("fork stage %s: cannot resolve child %s; child will use its current version", stage.name, child_type)
        return ""


def materialize_skeleton_tasks(
    skeleton: workflow_definitions.WorkflowSkeleton,
    *,
    workflow_id: str,
    tenant_id: str = "default",
    external_ref: str | None = None,
    stage_inputs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return task bodies ready for POST /workflow/tasks."""
    stage_inputs = stage_inputs or {}
    tasks: list[dict[str, Any]] = []
    for stage in skeleton.stages:
        # compensate_on_failure creates these tasks when a stage fails.
        if stage.kind == "compensate":
            continue
        agent_id = stage.agent_id
        if agent_id == "human":
            agent_id = "orchestrator"
        seeded_input = dict(stage_inputs.get(stage.name, {}))
        for key, expr in (stage.input_map or {}).items():
            token = str(expr or "").strip()
            if token and not token.startswith("$"):
                seeded_input.setdefault(key, token)
        approval_policy = dict(getattr(stage, "approval", None) or {})
        if approval_policy:
            seeded_input["_approval_policy"] = approval_policy
        child_version = _child_version(stage)
        if child_version:
            seeded_input.setdefault("_child_version", child_version)
        body: dict[str, Any] = {
            "step_name": stage.name,
            "agent_id": agent_id,
            "workflow_type": skeleton.workflow_type,
            "tenant_id": tenant_id,
            "depends_on": list(stage.depends_on),
            "input": seeded_input,
            "approval_required": stage.approval_required,
            "max_retries": int(stage.retry.get("max_retries", 0)),
            "definition_version": skeleton.definition_version,
            "contract": skeleton.resolve_stage_contract(stage) or {},
        }
        if external_ref and stage.name == skeleton.intake_stage_name():
            body["external_ref"] = external_ref
        tasks.append(body)
    return tasks


def resolve_depends_on_uuids(
    task_bodies: list[dict[str, Any]],
    created_ids: list[str],
) -> list[dict[str, Any]]:
    """Map depends_on step names to created task UUIDs for bulk insert."""
    name_to_id = {
        body["step_name"]: task_id
        for body, task_id in zip(task_bodies, created_ids, strict=True)
    }
    resolved: list[dict[str, Any]] = []
    for body, task_id in zip(task_bodies, created_ids, strict=True):
        updated = dict(body)
        updated["depends_on"] = [
            name_to_id[dep]
            for dep in (body.get("depends_on") or [])
            if dep in name_to_id
        ] or None
        resolved.append(updated)
    return resolved
