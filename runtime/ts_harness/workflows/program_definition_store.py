"""Versioned file store for managed program definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.definition_paths import program_content_revision
from ts_harness.workflows.versioned_definition_store import (
    ConflictError,
    VersionedDefinitionStore,
)

ensure_scripts_importable()

import program_definitions  # noqa: E402

__all__ = [
    "ConflictError",
    "ProgramDefinitionStore",
]


def _canonical_program(definition: dict[str, Any]) -> dict[str, Any]:
    loaded = program_definitions.load_program_payload(definition)
    return program_definitions.program_to_payload(loaded)


class ProgramDefinitionStore(VersionedDefinitionStore):
    def __init__(
        self,
        root: Path,
        *,
        built_in_types: set[str] | None = None,
    ) -> None:
        super().__init__(
            root,
            identity_field="program_type",
            canonicalize=_canonical_program,
            content_revision=program_content_revision,
            canonical_json=program_definitions.canonical_program_json,
            manifest_collection_key="programs",
            built_in_types=built_in_types,
            kind_label="program",
        )
