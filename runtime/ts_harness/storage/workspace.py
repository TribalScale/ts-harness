"""Agent workspaces: a directory under ``/workspace``, or ``workspaces/{agent_id}/`` in the object store.

Paths are always relative to the agent's workspace. The object backend keeps
text and binary files, caps each object, and can fill a local directory for
commands that need real files (the sandbox), then upload what changed.
"""

from __future__ import annotations

import os
import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_OBJECT_BYTES = 10 * 1024 * 1024
SANDBOX_MAX_FILES = 2000
SANDBOX_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_WORKSPACE_ROOT = "/workspace"


class WorkspaceTooLarge(ValueError):
    """The workspace is over the file-count or size cap for this operation."""


@dataclass(frozen=True)
class WorkspaceFile:
    path: str
    size: int


def workspace_root() -> Path:
    return Path(os.environ.get("TS_HARNESS_WORKSPACE_ROOT") or DEFAULT_WORKSPACE_ROOT)


def objects_enabled() -> bool:
    from ts_harness.storage.backend import object_backend

    return object_backend() != "local"


def clean_rel(path: str) -> str:
    """Normalize a workspace-relative path; reject anything that leaves the workspace."""
    text = str(path or "").replace("\\", "/").strip()
    if text.startswith("/"):
        raise ValueError("path escapes workspace")
    rel = posixpath.normpath(text) if text else "."
    if rel == ".." or rel.startswith("../"):
        raise ValueError("path escapes workspace")
    return "" if rel == "." else rel


class WorkspaceStore(ABC):
    def __init__(self, agent_id: str) -> None:
        if not agent_id or "/" in agent_id or agent_id in {".", ".."}:
            raise ValueError(f"invalid agent id: {agent_id!r}")
        self.agent_id = agent_id

    def display_path(self, rel: str) -> str:
        return f"{DEFAULT_WORKSPACE_ROOT}/{self.agent_id}" + (f"/{rel}" if rel else "")

    @abstractmethod
    def read_bytes(self, rel: str) -> bytes:
        """Raises FileNotFoundError when missing."""

    @abstractmethod
    def write_bytes(self, rel: str, data: bytes) -> int: ...

    @abstractmethod
    def exists(self, rel: str) -> bool: ...

    @abstractmethod
    def is_file(self, rel: str) -> bool: ...

    @abstractmethod
    def list(self, rel: str = "") -> list[WorkspaceFile]:
        """Files at or under ``rel``, sorted by path."""

    @abstractmethod
    def delete(self, rel: str) -> None: ...

    def read_text(self, rel: str) -> str:
        return self.read_bytes(rel).decode("utf-8")

    def write_text(self, rel: str, text: str) -> int:
        return self.write_bytes(rel, text.encode("utf-8"))

    def read_text_or_none(self, rel: str) -> str | None:
        try:
            return self.read_text(rel)
        except FileNotFoundError:
            return None


class FileWorkspace(WorkspaceStore):
    """Today's behavior: plain files under ``{root}/{agent_id}``."""

    def __init__(self, agent_id: str, root: str | Path | None = None) -> None:
        super().__init__(agent_id)
        self.base = Path(root or workspace_root()) / agent_id

    def _path(self, rel: str) -> Path:
        return self.base / clean_rel(rel)

    def display_path(self, rel: str) -> str:
        return str(self._path(rel))

    def read_bytes(self, rel: str) -> bytes:
        path = self._path(rel)
        if not path.is_file():
            raise FileNotFoundError(rel)
        return path.read_bytes()

    def write_bytes(self, rel: str, data: bytes) -> int:
        path = self._path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return len(data)

    def exists(self, rel: str) -> bool:
        return self._path(rel).exists()

    def is_file(self, rel: str) -> bool:
        return self._path(rel).is_file()

    def list(self, rel: str = "") -> list[WorkspaceFile]:
        start = self._path(rel)
        if start.is_file():
            return [WorkspaceFile(clean_rel(rel), start.stat().st_size)]
        if not start.is_dir():
            return []
        return [
            WorkspaceFile(p.relative_to(self.base).as_posix(), p.stat().st_size)
            for p in sorted(start.rglob("*"))
            if p.is_file()
        ]

    def delete(self, rel: str) -> None:
        self._path(rel).unlink(missing_ok=True)


class ObjectWorkspace(WorkspaceStore):
    """Files under ``workspaces/{agent_id}/`` in the workspaces bucket."""

    def __init__(self, agent_id: str, objects: Any = None, *, max_object_bytes: int = MAX_OBJECT_BYTES) -> None:
        super().__init__(agent_id)
        self._objects = objects
        self.prefix = f"workspaces/{agent_id}/"
        self.max_object_bytes = max_object_bytes

    @property
    def objects(self) -> Any:
        if self._objects is None:
            from ts_harness.storage.objects import object_store

            self._objects = object_store("workspaces")
        return self._objects

    def _key(self, rel: str) -> str:
        cleaned = clean_rel(rel)
        if not cleaned:
            raise IsADirectoryError("the workspace root is a directory")
        return self.prefix + cleaned

    def read_bytes(self, rel: str) -> bytes:
        from ts_harness.storage.objects import ObjectNotFound

        try:
            return self.objects.get(self._key(rel))
        except ObjectNotFound as exc:
            raise FileNotFoundError(rel) from exc

    def write_bytes(self, rel: str, data: bytes) -> int:
        if len(data) > self.max_object_bytes:
            raise WorkspaceTooLarge(f"file is larger than {self.max_object_bytes // (1024 * 1024)} MB: {rel}")
        self.objects.put(self._key(rel), data)
        return len(data)

    def exists(self, rel: str) -> bool:
        return self.is_file(rel) or bool(self.list(rel))

    def is_file(self, rel: str) -> bool:
        cleaned = clean_rel(rel)
        return bool(cleaned) and self.objects.head(self.prefix + cleaned) is not None

    def list(self, rel: str = "") -> list[WorkspaceFile]:
        cleaned = clean_rel(rel)
        if cleaned and self.is_file(cleaned):
            info = self.objects.head(self.prefix + cleaned)
            return [WorkspaceFile(cleaned, info.size)]
        prefix = self.prefix + (f"{cleaned}/" if cleaned else "")
        return [WorkspaceFile(info.key[len(self.prefix):], info.size) for info in self.objects.list(prefix)]

    def delete(self, rel: str) -> None:
        self.objects.delete(self._key(rel))

    def fill(self, dest: Path, *, max_files: int = SANDBOX_MAX_FILES, max_bytes: int = SANDBOX_MAX_BYTES) -> dict[str, bytes]:
        """Download the whole workspace into ``dest``; returns what was written, for ``upload_changes``."""
        files = self.list()
        total = sum(f.size for f in files)
        if len(files) > max_files or total > max_bytes:
            raise WorkspaceTooLarge(
                f"workspace has {len(files)} files and {total // (1024 * 1024)} MB; "
                f"the sandbox takes at most {max_files} files and {max_bytes // (1024 * 1024)} MB"
            )
        written: dict[str, bytes] = {}
        for item in files:
            data = self.read_bytes(item.path)
            target = dest / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written[item.path] = data
        return written

    def upload_changes(
        self, src: Path, before: dict[str, bytes], *, max_files: int = SANDBOX_MAX_FILES, max_bytes: int = SANDBOX_MAX_BYTES
    ) -> dict[str, Any]:
        """Upload files under ``src`` that are new or changed since ``fill``, and delete ones that were removed."""
        changed: list[tuple[str, bytes]] = []
        present: set[str] = set()
        for path in sorted(src.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(src).as_posix()
            present.add(rel)
            data = path.read_bytes()
            if before.get(rel) != data:
                changed.append((rel, data))
        uploaded, skipped, total = [], [], 0
        for rel, data in changed:
            if len(uploaded) >= max_files or total + len(data) > max_bytes or len(data) > self.max_object_bytes:
                skipped.append(rel)
                continue
            try:
                self.objects.put(self.prefix + rel, data)
            except ValueError:
                skipped.append(rel)
                continue
            uploaded.append(rel)
            total += len(data)
        deleted = sorted(set(before) - present)
        for rel in deleted:
            self.objects.delete(self.prefix + rel)
        return {"uploaded": uploaded, "deleted": deleted, "skipped": skipped}


def workspace_store(agent_id: str, *, root: str | Path | None = None) -> WorkspaceStore:
    """The object workspace when an object backend is on, else the directory under ``root``."""
    if objects_enabled():
        return ObjectWorkspace(agent_id)
    return FileWorkspace(agent_id, root)
