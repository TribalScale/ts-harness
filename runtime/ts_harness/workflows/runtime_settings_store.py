"""Versioned store for managed runtime-settings overlays."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ts_harness.config.runtime_settings import (
    validate_runtime_settings_payload,
)
from ts_harness.workflows.versioned_definition_store import (
    ConflictError,
    VersionedDefinitionStore,
)

__all__ = [
    "ConflictError",
    "RuntimeSettingsStore",
    "runtime_settings_store_root",
    "runtime_settings_store",
]


def runtime_settings_store_root() -> Path:
    override = os.environ.get("TS_HARNESS_RUNTIME_SETTINGS_STORE")
    if override:
        return Path(override)
    workflow_root = os.environ.get("TS_HARNESS_WORKFLOW_DEFINITION_STORE")
    if workflow_root:
        return Path(workflow_root) / "runtime-settings"
    return Path("workflow-definitions") / "runtime-settings"


def _canonical_settings(definition: dict[str, Any]) -> dict[str, Any]:
    canonical, issues = validate_runtime_settings_payload(definition)
    errors = [issue for issue in issues if issue.get("severity") == "error"]
    if errors or canonical is None:
        message = "; ".join(issue["message"] for issue in errors) or "invalid runtime settings"
        raise ValueError(message)
    return canonical


def _settings_content_revision(definition: dict[str, Any]) -> str:
    text = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_settings_json(definition: dict[str, Any]) -> str:
    return json.dumps(_canonical_settings(definition), indent=2, sort_keys=True) + "\n"


class RuntimeSettingsStore(VersionedDefinitionStore):
    def __init__(self, root: Path | None = None) -> None:
        super().__init__(
            root or runtime_settings_store_root(),
            identity_field="definition_type",
            canonicalize=_canonical_settings,
            content_revision=_settings_content_revision,
            canonical_json=_canonical_settings_json,
            manifest_collection_key="runtime_settings",
            built_in_types=set(),
            kind_label="runtime settings",
        )

    def list_drafts(self) -> list[dict[str, Any]]:
        drafts: list[dict[str, Any]] = []
        for path in self.drafts_dir.glob("*.json"):
            envelope = self._read_json(path)
            if envelope.get("status") == "draft":
                drafts.append(envelope)
        drafts.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return drafts


_STORE: RuntimeSettingsStore | None = None


def runtime_settings_store() -> RuntimeSettingsStore:
    global _STORE
    if _STORE is None:
        _STORE = RuntimeSettingsStore()
    return _STORE
