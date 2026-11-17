"""Hot-reloadable registry of active and built-in program definitions."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable, programs_dir
from ts_harness.workflows.definition_paths import (
    list_history_paths_for_version,
    program_content_revision,
)
from ts_harness.workflows.program_definition_store import ProgramDefinitionStore

ensure_scripts_importable()

import program_definitions  # noqa: E402

logger = logging.getLogger(__name__)


def program_definition_store_root() -> Path:
    return Path(
        os.environ.get(
            "TS_HARNESS_PROGRAM_DEFINITION_STORE",
            "program-definitions",
        )
    )


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _builtin_payloads() -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    root = programs_dir()
    if not root.exists():
        return payloads
    for path in sorted(root.glob("*.json")):
        if path.name in {"schema.json", "manifest.json"}:
            continue
        raw = _read_json_object(path)
        if raw is None:
            continue
        try:
            loaded = program_definitions.load_program_payload(raw)
            payloads[loaded.program_type] = program_definitions.program_to_payload(
                loaded
            )
        except ValueError:
            logger.exception("skipping invalid built-in program definition: %s", path)
    return payloads


class ProgramDefinitionRegistry:
    """Process-wide cache of program definitions with fail-closed reload."""

    def __init__(self, store_root: Path | None = None) -> None:
        self._store_root = Path(store_root) if store_root is not None else None
        self._definitions: dict[str, dict[str, Any]] = {}
        self._versioned: dict[tuple[str, str], dict[str, Any]] = {}
        self._generation: int | None = None
        self.reload_definitions()

    @property
    def generation(self) -> int:
        return int(self._generation or 0)

    def _resolved_store_root(self) -> Path:
        if self._store_root is not None:
            return self._store_root
        return program_definition_store_root()

    def _store(self) -> ProgramDefinitionStore:
        builtins = set(_builtin_payloads())
        return ProgramDefinitionStore(
            self._resolved_store_root(),
            built_in_types=builtins,
        )

    def _manifest_path(self) -> Path:
        return self._resolved_store_root() / "active" / "manifest.json"

    def _read_manifest_generation(self) -> int | None:
        path = self._manifest_path()
        if not path.exists():
            return 0
        data = _read_json_object(path)
        if data is None:
            raise ValueError("program definition manifest must be an object")
        return int(data.get("generation") or 0)

    def reload_definitions(self) -> bool:
        try:
            builtins = _builtin_payloads()
            definitions = dict(builtins)
            store = self._store()
            for summary in store.list_active():
                program_type = str(summary.get("program_type") or "").strip()
                if not program_type:
                    continue
                active = store.get_active(program_type)
                if active is None:
                    continue
                loaded = program_definitions.load_program_payload(active)
                definitions[loaded.program_type] = program_definitions.program_to_payload(
                    loaded
                )
            generation = self._read_manifest_generation()
        except Exception:
            logger.exception(
                "program definition reload failed; keeping last good set"
            )
            return False

        for program_type, payload in self._definitions.items():
            version = str(payload.get("definition_version") or "").strip()
            if version:
                self._versioned.setdefault((program_type, version), payload)
        self._definitions = definitions
        self._generation = generation
        return True

    def reload_if_changed(self) -> bool:
        from ts_harness.storage.shared_tree import sync_definitions

        sync_definitions()
        try:
            generation = self._read_manifest_generation()
        except Exception:
            logger.exception("program definition manifest read failed")
            return False
        if generation == self._generation:
            return True
        return self.reload_definitions()

    def list_types(self) -> list[str]:
        return sorted(self._definitions)

    def load(
        self,
        program_type: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        key = str(program_type or "").strip()
        if not key:
            raise ValueError("program_type is required")
        requested = str(version or "").strip()
        current = self._definitions.get(key)
        if current is None and not requested:
            raise FileNotFoundError(f"program definition not found: {key}")
        if current is not None and (
            not requested
            or requested == str(current.get("definition_version") or "")
        ):
            return dict(current)

        cache_key = (key, requested)
        if requested and cache_key in self._versioned:
            return dict(self._versioned[cache_key])

        if requested:
            store_root = self._resolved_store_root()
            matches = list_history_paths_for_version(
                store_root,
                key,
                requested,
                field="program_type",
            )
            for path in reversed(matches):
                payload = _read_json_object(path)
                if payload is None:
                    continue
                try:
                    loaded = program_definitions.load_program_payload(payload)
                except ValueError:
                    continue
                if loaded.program_type != key:
                    continue
                if str(loaded.definition_version) != requested:
                    continue
                canonical = program_definitions.program_to_payload(loaded)
                self._versioned[cache_key] = canonical
                return dict(canonical)
            if current is not None and requested == str(
                current.get("definition_version") or ""
            ):
                return dict(current)
            raise FileNotFoundError(
                f"program definition version not found: "
                f"program_type={key} definition_version={requested}"
            )

        raise FileNotFoundError(f"program definition not found: {key}")

    def content_revision(self, program_type: str, version: str | None = None) -> str:
        payload = self.load(program_type, version=version)
        return program_content_revision(payload)


_default_registry: ProgramDefinitionRegistry | None = None


def get_program_definition_registry() -> ProgramDefinitionRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = ProgramDefinitionRegistry()
    return _default_registry


def reset_program_definition_registry_for_tests() -> None:
    global _default_registry
    _default_registry = None
