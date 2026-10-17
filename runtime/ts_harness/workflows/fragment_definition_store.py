"""Versioned file store for managed workflow stage fragments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.versioned_definition_store import (
    ConflictError,
    VersionedDefinitionStore,
)

ensure_scripts_importable()

import workflow_fragments  # noqa: E402

__all__ = [
    "ConflictError",
    "FragmentDefinitionStore",
]


def _canonical_fragment(definition: dict[str, Any]) -> dict[str, Any]:
    fragment = workflow_fragments.load_fragment_payload(definition)
    return workflow_fragments.fragment_to_payload(fragment)


class FragmentDefinitionStore(VersionedDefinitionStore):
    def __init__(
        self,
        root: Path,
        *,
        built_in_types: set[str] | None = None,
    ) -> None:
        super().__init__(
            root,
            identity_field="fragment_id",
            canonicalize=_canonical_fragment,
            content_revision=workflow_fragments.fragment_content_revision,
            canonical_json=workflow_fragments.canonical_fragment_json,
            manifest_collection_key="fragments",
            built_in_types=built_in_types,
            kind_label="fragment",
        )
