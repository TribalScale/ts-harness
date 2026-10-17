"""Versioned store for managed channel routing overlays."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ts_harness.routing.overlay import validate_overlay_payload
from ts_harness.workflows.versioned_definition_store import (
    ConflictError,
    VersionedDefinitionStore,
)

__all__ = [
    "ConflictError",
    "RoutingDefinitionStore",
    "routing_definition_store_root",
    "routing_definition_store",
]


def routing_definition_store_root() -> Path:
    override = os.environ.get("TS_HARNESS_ROUTING_DEFINITION_STORE")
    if override:
        return Path(override)
    workflow_root = os.environ.get("TS_HARNESS_WORKFLOW_DEFINITION_STORE")
    if workflow_root:
        return Path(workflow_root) / "routing"
    return Path("workflow-definitions") / "routing"


def _canonical_routing(definition: dict[str, Any]) -> dict[str, Any]:
    # Structural canonicalize; activate re-validates against live catalogs.
    canonical, issues = validate_overlay_payload(
        definition,
        known_program_types=set(),
        known_workflow_types=set(),
    )
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        message = "; ".join(issue["message"] for issue in errors) or "invalid routing"
        raise ValueError(message)
    return canonical


def _routing_content_revision(definition: dict[str, Any]) -> str:
    text = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_routing_json(definition: dict[str, Any]) -> str:
    return json.dumps(_canonical_routing(definition), indent=2, sort_keys=True) + "\n"


class RoutingDefinitionStore(VersionedDefinitionStore):
    def __init__(self, root: Path | None = None) -> None:
        super().__init__(
            root or routing_definition_store_root(),
            identity_field="definition_type",
            canonicalize=_canonical_routing,
            content_revision=_routing_content_revision,
            canonical_json=_canonical_routing_json,
            manifest_collection_key="routing",
            built_in_types=set(),
            kind_label="routing",
        )

    def list_drafts(self) -> list[dict[str, Any]]:
        drafts: list[dict[str, Any]] = []
        for path in self.drafts_dir.glob("*.json"):
            envelope = self._read_json(path)
            if envelope.get("status") == "draft":
                drafts.append(envelope)
        drafts.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return drafts


_STORE: RoutingDefinitionStore | None = None


def routing_definition_store() -> RoutingDefinitionStore:
    global _STORE
    if _STORE is None:
        _STORE = RoutingDefinitionStore()
    return _STORE
