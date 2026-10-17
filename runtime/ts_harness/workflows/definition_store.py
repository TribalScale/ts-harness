"""Versioned file store for managed workflow definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.definition_paths import definition_content_revision
from ts_harness.workflows.versioned_definition_store import (
    ConflictError,
    VersionedDefinitionStore,
)

ensure_scripts_importable()

import workflow_definitions  # noqa: E402

__all__ = [
    "ConflictError",
    "WorkflowDefinitionStore",
]


def _canonical_definition(definition: dict[str, Any]) -> dict[str, Any]:
    skeleton = workflow_definitions.load_skeleton_payload(definition)
    return workflow_definitions.skeleton_to_payload(skeleton)


class WorkflowDefinitionStore(VersionedDefinitionStore):
    def __init__(
        self,
        root: Path,
        *,
        built_in_types: set[str] | None = None,
    ) -> None:
        super().__init__(
            root,
            identity_field="workflow_type",
            canonicalize=_canonical_definition,
            content_revision=definition_content_revision,
            canonical_json=workflow_definitions.canonical_definition_json,
            manifest_collection_key="workflows",
            built_in_types=built_in_types,
            kind_label="workflow",
        )
