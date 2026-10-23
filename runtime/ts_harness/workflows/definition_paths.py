"""Safe filesystem paths for managed workflow definitions."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import tempfile
from typing import Any

from ts_harness.repo_paths import ensure_scripts_importable

ensure_scripts_importable()

import program_definitions  # noqa: E402
import workflow_definitions  # noqa: E402

logger = logging.getLogger(__name__)


def safe_component(value: str, *, field: str = "path") -> str:
    token = str(value or "").strip()
    if not token or "/" in token or "\\" in token or ".." in token:
        raise ValueError(f"{field} contains an unsafe path component")
    return token


def safe_definition_version(value: str) -> str:
    return safe_component(
        str(value).replace(":", "_"),
        field="definition_version",
    )


def definition_version_key(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def history_filename(definition_version: str, revision: str) -> str:
    safe_revision = safe_component(revision, field="revision")
    return f"{definition_version_key(definition_version)}-{safe_revision}.json"


def history_filename_prefixes(definition_version: str) -> list[str]:
    current = f"{definition_version_key(definition_version)}-"
    legacy = f"{safe_definition_version(definition_version)}-"
    if legacy == current:
        return [current]
    return [current, legacy]


def history_dir(
    store_root: Path,
    identity: str,
    *,
    field: str = "workflow_type",
) -> Path:
    return Path(store_root) / "history" / safe_component(
        identity,
        field=field,
    )


def program_definition_store_root(volume_root: Path) -> Path:
    """Return the programs namespace root under a shared volume."""
    return Path(volume_root) / "programs"


def program_history_dir(volume_root: Path, program_type: str) -> Path:
    return history_dir(
        program_definition_store_root(volume_root),
        program_type,
        field="program_type",
    )


def _history_payload_version(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("definition_version") or "").strip()
    return version or None


def list_history_paths_for_version(
    store_root: Path,
    identity: str,
    definition_version: str,
    *,
    field: str = "workflow_type",
) -> list[Path]:
    """Return history files for an exact definition_version.

    Hash-prefixed names are accepted by filename. Legacy sanitized prefixes are
    accepted only when the file payload version matches exactly, so `v1` does
    not collide with `v1-beta`.
    """
    root = history_dir(store_root, identity, field=field)
    if not root.exists():
        return []
    requested = str(definition_version)
    hash_prefix = f"{definition_version_key(requested)}-"
    legacy_prefix = f"{safe_definition_version(requested)}-"
    matches: list[Path] = []
    for path in sorted(root.glob("*.json")):
        name = path.name
        if name.startswith(hash_prefix):
            matches.append(path)
            continue
        if legacy_prefix != hash_prefix and name.startswith(legacy_prefix):
            if _history_payload_version(path) == requested:
                matches.append(path)
    return matches


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    tmp.replace(path)


def definition_content_revision(payload: dict[str, Any]) -> str:
    text = workflow_definitions.canonical_definition_json(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def program_content_revision(payload: dict[str, Any]) -> str:
    text = program_definitions.canonical_program_json(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_history_snapshot(
    store_root: Path,
    skeleton_payload_or_skeleton: Any,
) -> Path:
    if isinstance(skeleton_payload_or_skeleton, dict):
        skeleton = workflow_definitions.load_skeleton_payload(
            skeleton_payload_or_skeleton
        )
    else:
        skeleton = skeleton_payload_or_skeleton
    payload = workflow_definitions.skeleton_to_payload(skeleton)
    revision = definition_content_revision(payload)
    filename = history_filename(
        str(skeleton.definition_version),
        revision,
    )
    path = history_dir(Path(store_root), skeleton.workflow_type) / filename
    try:
        if not path.exists():
            _atomic_write_json(path, payload)
    except (OSError, PermissionError):
        logger.exception(
            "workflow history snapshot write failed "
            "workflow_type=%s definition_version=%s",
            skeleton.workflow_type,
            skeleton.definition_version,
        )
    return path
