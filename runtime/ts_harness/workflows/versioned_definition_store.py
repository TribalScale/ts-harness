"""Generic versioned draft/active/history definition store."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Iterator
import uuid

from ts_harness.workflows.definition_paths import (
    definition_version_key,
    history_dir,
    history_filename,
    list_history_paths_for_version,
    safe_component,
    safe_definition_version,
)

_LOCKFILE_STALE_SECONDS = 60.0

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX platforms
    fcntl = None

_UNSET: Any = object()
_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[Path, threading.RLock] = {}


class ConflictError(RuntimeError):
    """Raised when an optimistic revision check fails."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lock_for_root(root: Path) -> threading.RLock:
    key = root.resolve()
    with _LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_LOCKS[key] = lock
        return lock


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


def _validate_editor_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("editor_metadata must be an object")
    return value


class VersionedDefinitionStore:
    """Draft/active/history store parameterized by identity and canonicalization."""

    def __init__(
        self,
        root: Path,
        *,
        identity_field: str,
        canonicalize: Callable[[dict[str, Any]], dict[str, Any]],
        content_revision: Callable[[dict[str, Any]], str],
        canonical_json: Callable[[dict[str, Any]], str],
        manifest_collection_key: str,
        built_in_types: set[str] | None = None,
        kind_label: str,
    ) -> None:
        self.root = Path(root)
        self.identity_field = identity_field
        self._canonicalize = canonicalize
        self._content_revision = content_revision
        self._canonical_json = canonical_json
        self.manifest_collection_key = manifest_collection_key
        self.built_in_types = set(built_in_types or set())
        self.kind_label = kind_label
        self._thread_lock = _lock_for_root(self.root)

    @property
    def drafts_dir(self) -> Path:
        return self.root / "drafts"

    @property
    def active_dir(self) -> Path:
        return self.root / "active"

    @property
    def history_dir(self) -> Path:
        return self.root / "history"

    def _draft_revision(
        self,
        definition: dict[str, Any],
        editor_metadata: dict[str, Any] | None = None,
    ) -> str:
        definition_text = self._canonical_json(definition)
        metadata_text = json.dumps(
            editor_metadata or {},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(
            f"{definition_text}\n{metadata_text}".encode("utf-8")
        ).hexdigest()

    def _summary_from_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": envelope["id"],
            self.identity_field: envelope[self.identity_field],
            "status": envelope["status"],
            "revision": envelope["revision"],
            "base_revision": envelope.get("base_revision"),
            "created_at": envelope["created_at"],
            "updated_at": envelope["updated_at"],
            "created_by": envelope["created_by"],
            "updated_by": envelope["updated_by"],
            "definition_version": envelope["definition"]["definition_version"],
        }

    def _draft_path(self, draft_id: str) -> Path:
        safe_id = safe_component(draft_id, field="draft_id")
        return self.drafts_dir / f"{safe_id}.json"

    def _active_path(self, identity: str) -> Path:
        safe_type = safe_component(identity, field=self.identity_field)
        return self.active_dir / f"{safe_type}.json"

    def _history_path(
        self,
        identity: str,
        definition_version: str,
        revision: str,
    ) -> Path:
        return history_dir(
            self.root,
            identity,
            field=self.identity_field,
        ) / history_filename(
            definition_version,
            revision,
        )

    def _history_version_revisions(
        self,
        identity: str,
        definition_version: str,
    ) -> set[str]:
        revisions: set[str] = set()
        hash_prefix = f"{definition_version_key(definition_version)}-"
        legacy_prefix = f"{safe_definition_version(definition_version)}-"
        for path in list_history_paths_for_version(
            self.root,
            identity,
            definition_version,
            field=self.identity_field,
        ):
            name = path.name
            if name.startswith(hash_prefix) and name.endswith(".json"):
                revisions.add(name[len(hash_prefix) : -5])
            elif name.startswith(legacy_prefix) and name.endswith(".json"):
                revisions.add(name[len(legacy_prefix) : -5])
        return revisions

    @contextmanager
    def _store_lock(self) -> Iterator[None]:
        from ts_harness.storage.shared_tree import definitions_tree_for

        shared = definitions_tree_for(self.root)
        if shared is not None:
            tree, rel = shared
            prefix = "" if rel == "." else f"{rel}/"
            with self._thread_lock, tree.transaction(
                f"store:{rel if rel != '.' else 'root'}",
                [f"{prefix}drafts", f"{prefix}active", f"{prefix}history"],
            ):
                yield
            return
        with self._thread_lock:
            lock_path = self.root / ".store.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    acquired_lockfile = None
                else:
                    acquired_lockfile = self.root / ".store.lock.exclusive"
                    deadline = time.monotonic() + 30
                    fd = None
                    while fd is None:
                        try:
                            fd = os.open(
                                acquired_lockfile,
                                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                            )
                        except FileExistsError:
                            try:
                                age = time.time() - acquired_lockfile.stat().st_mtime
                            except FileNotFoundError:
                                age = 0
                            if age >= _LOCKFILE_STALE_SECONDS:
                                try:
                                    acquired_lockfile.unlink()
                                except FileNotFoundError:
                                    pass
                                continue
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    f"timed out acquiring store lock: {acquired_lockfile}"
                                )
                            time.sleep(0.05)
                    os.close(fd)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    elif acquired_lockfile is not None:
                        try:
                            acquired_lockfile.unlink()
                        except FileNotFoundError:
                            pass

    def list_definitions(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for path in sorted(self.drafts_dir.glob("*.json")):
            summaries.append(self._summary_from_envelope(self._read_json(path)))
        for active in self.list_active():
            summaries.append({"status": "active", **active})
        return summaries

    def create_draft(
        self,
        definition: dict[str, Any],
        *,
        actor_id: str,
        editor_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._store_lock():
            canonical = self._canonicalize(definition)
            identity = safe_component(
                canonical[self.identity_field],
                field=self.identity_field,
            )
            draft_id = str(uuid.uuid4())
            now = _utc_now()
            metadata = (
                _validate_editor_metadata(editor_metadata)
                if editor_metadata is not None
                else None
            )
            envelope = {
                "id": draft_id,
                self.identity_field: identity,
                "status": "draft",
                "revision": self._draft_revision(canonical, metadata),
                "base_revision": None,
                "created_at": now,
                "updated_at": now,
                "created_by": actor_id,
                "updated_by": actor_id,
                "definition": canonical,
            }
            if metadata is not None:
                envelope["editor_metadata"] = metadata
            _atomic_write_json(self._draft_path(draft_id), envelope)
            return envelope

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self._read_json(self._draft_path(draft_id))

    def update_draft(
        self,
        draft_id: str,
        definition: dict[str, Any],
        *,
        expected_revision: str,
        actor_id: str,
        editor_metadata: Any = _UNSET,
    ) -> dict[str, Any]:
        with self._store_lock():
            path = self._draft_path(draft_id)
            envelope = self._read_json(path)
            if envelope["revision"] != expected_revision:
                raise ConflictError("draft revision mismatch")

            canonical = self._canonicalize(definition)
            identity = safe_component(
                canonical[self.identity_field],
                field=self.identity_field,
            )
            metadata = (
                _validate_editor_metadata(editor_metadata)
                if editor_metadata is not _UNSET
                else envelope.get("editor_metadata")
            )
            updated = dict(envelope)
            updated.update(
                {
                    self.identity_field: identity,
                    "revision": self._draft_revision(canonical, metadata),
                    "updated_at": _utc_now(),
                    "updated_by": actor_id,
                    "definition": canonical,
                }
            )
            if metadata is not None:
                updated["editor_metadata"] = metadata
            elif "editor_metadata" in updated:
                del updated["editor_metadata"]
            _atomic_write_json(path, updated)
            return updated

    def delete_draft(self, draft_id: str) -> None:
        with self._store_lock():
            self._draft_path(draft_id).unlink()

    def activate(
        self,
        draft_id: str,
        *,
        expected_revision: str,
        actor_id: str,
        built_in_types: set[str] | None = None,
    ) -> dict[str, Any]:
        with self._store_lock():
            draft_path = self._draft_path(draft_id)
            envelope = self._read_json(draft_path)
            if envelope["revision"] != expected_revision:
                raise ConflictError("draft revision mismatch")

            definition = dict(envelope["definition"])
            identity = safe_component(
                definition[self.identity_field],
                field=self.identity_field,
            )
            blocked_types = self.built_in_types | set(built_in_types or set())
            if identity in blocked_types:
                raise ValueError(
                    f"cannot activate over built-in {self.kind_label}: {identity}"
                )

            definition_version = str(definition["definition_version"])
            revision = self._content_revision(definition)
            history_revisions = self._history_version_revisions(
                identity,
                definition_version,
            )
            if history_revisions and history_revisions != {revision}:
                raise ValueError(
                    "cannot reuse historical definition_version "
                    f"for {identity}: {definition_version}"
                )

            active = self.get_active(identity)
            if (
                active
                and active.get("definition_version") == definition_version
                and not history_revisions
                and self._content_revision(active) != revision
            ):
                raise ValueError(
                    "cannot activate the same definition_version twice "
                    f"for {identity}: {definition_version}"
                )

            activated_at = _utc_now()
            history_path = self._history_path(
                identity,
                definition_version,
                revision,
            )
            if not history_path.exists():
                _atomic_write_json(
                    history_path,
                    definition,
                )
            manifest = self.read_manifest()
            collection = manifest.setdefault(self.manifest_collection_key, {})
            previous = dict(collection.get(identity) or {})
            if (
                previous.get("definition_version") != definition_version
                or previous.get("revision") != revision
            ):
                manifest["generation"] = int(manifest.get("generation") or 0) + 1
            collection[identity] = {
                "definition_version": definition_version,
                "revision": revision,
                "activated_at": activated_at,
                "activated_by": actor_id,
            }
            _atomic_write_json(self.active_dir / "manifest.json", manifest)
            _atomic_write_json(self._active_path(identity), definition)
            try:
                draft_path.unlink()
            except FileNotFoundError:
                pass
            return {
                self.identity_field: identity,
                "definition_version": definition_version,
                "revision": revision,
                "activated_at": activated_at,
                "activated_by": actor_id,
            }

    def get_active(self, identity: str) -> dict[str, Any] | None:
        path = self._active_path(identity)
        active = self._read_json(path) if path.exists() else None
        manifest = self.read_manifest()
        meta = dict(
            (manifest.get(self.manifest_collection_key) or {}).get(identity) or {}
        )
        if not meta:
            return active
        revision = str(meta.get("revision") or "").strip()
        definition_version = str(meta.get("definition_version") or "").strip()
        if not revision or not definition_version:
            return None
        if (
            active is not None
            and active.get("definition_version") == definition_version
            and self._content_revision(active) == revision
        ):
            return active
        history_path = self._history_path(
            identity,
            definition_version,
            revision,
        )
        if history_path.exists():
            return self._read_json(history_path)
        for path in list_history_paths_for_version(
            self.root,
            identity,
            definition_version,
            field=self.identity_field,
        ):
            if path.name.endswith(f"-{revision}.json"):
                return self._read_json(path)
        return None

    def get_active_revision(self, identity: str) -> str | None:
        for item in self.list_active():
            if str(item.get(self.identity_field) or "") == identity:
                revision = item.get("revision")
                return str(revision) if revision else None
        return None

    def list_active(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        manifest = self.read_manifest()
        collection = dict(manifest.get(self.manifest_collection_key) or {})
        for identity, meta in sorted(collection.items()):
            summaries.append(
                {
                    self.identity_field: identity,
                    "definition_version": meta.get("definition_version"),
                    "revision": meta.get("revision"),
                    "activated_at": meta.get("activated_at"),
                    "activated_by": meta.get("activated_by"),
                }
            )
        return summaries

    def read_manifest(self) -> dict[str, Any]:
        path = self.active_dir / "manifest.json"
        if not path.exists():
            return {"generation": 0, self.manifest_collection_key: {}}
        data = self._read_json(path)
        data.setdefault("generation", 0)
        data.setdefault(self.manifest_collection_key, {})
        return data

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object: {path}")
        return data
