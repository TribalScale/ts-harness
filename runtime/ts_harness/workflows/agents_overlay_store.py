"""Versioned store for overlay-only specialist agents."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ts_harness.config.agents_overlay import validate_agents_overlay
from ts_harness.workflows.versioned_definition_store import (
    ConflictError,
    VersionedDefinitionStore,
)

__all__ = [
    "AgentsOverlayStore",
    "ConflictError",
    "agents_overlay_store",
    "agents_overlay_store_root",
]


def agents_overlay_store_root() -> Path:
    override = os.environ.get("TS_HARNESS_AGENTS_STORE")
    if override:
        return Path(override)
    workflow_root = os.environ.get("TS_HARNESS_WORKFLOW_DEFINITION_STORE")
    if workflow_root:
        return Path(workflow_root) / "agents"
    return Path("workflow-definitions") / "agents"


def _runtime_model_allowlist() -> dict[str, list[str]] | None:
    path = os.environ.get("TS_HARNESS_CONFIG", "").strip()
    if not path:
        return None
    try:
        from ts_harness.config.schema import load_runtime_config

        return dict(load_runtime_config(path).policy.model_allowlist)
    except Exception:
        return None


def _canonical_agents(definition: dict[str, Any]) -> dict[str, Any]:
    canonical, issues = validate_agents_overlay(
        definition,
        yaml_agent_ids=set(),
        toolset_keys=None,
        catalog=None,
        model_allowlist=_runtime_model_allowlist(),
    )
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        message = "; ".join(issue["message"] for issue in errors) or "invalid agents overlay"
        raise ValueError(message)
    return canonical


def _agents_content_revision(definition: dict[str, Any]) -> str:
    text = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_agents_json(definition: dict[str, Any]) -> str:
    return json.dumps(_canonical_agents(definition), indent=2, sort_keys=True) + "\n"


class AgentsOverlayStore(VersionedDefinitionStore):
    def __init__(self, root: Path | None = None) -> None:
        super().__init__(
            root or agents_overlay_store_root(),
            identity_field="definition_type",
            canonicalize=_canonical_agents,
            content_revision=_agents_content_revision,
            canonical_json=_canonical_agents_json,
            manifest_collection_key="agents",
            built_in_types=set(),
            kind_label="agents overlay",
        )

    def list_drafts(self) -> list[dict[str, Any]]:
        drafts: list[dict[str, Any]] = []
        for path in self.drafts_dir.glob("*.json"):
            envelope = self._read_json(path)
            if envelope.get("status") == "draft":
                drafts.append(envelope)
        drafts.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return drafts


_STORE: AgentsOverlayStore | None = None


def agents_overlay_store() -> AgentsOverlayStore:
    global _STORE
    if _STORE is None:
        _STORE = AgentsOverlayStore()
    return _STORE
