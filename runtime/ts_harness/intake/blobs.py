"""Intake file bytes in the ``intake`` object store (local directory, S3 or GCS).

Keys:

- ``intake/{tenant}/{sha[:2]}/{sha}``: console uploads, content-addressed so a file
  uploaded twice is stored once.
- ``intake/{tenant}/discord/{attachment_id}``: Discord prefetch copies.

Objects only need to live until the gather step reads them. S3 and GCS expire
``intake/`` with a bucket lifecycle rule; the local backend is swept by
``sweep_local_intake``.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import time
from typing import Any, BinaryIO

from ts_harness.intake.sources import INTAKE_RETENTION_DAYS, MAX_FILE_BYTES, guess_mime
from ts_harness.storage.objects import LocalObjectStore, ObjectStore, check_key, object_store

INTAKE_PREFIX = "intake/"
_CHUNK = 64 * 1024
_SPOOL_BYTES = 1024 * 1024
_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_NAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")


class UploadTooLarge(ValueError):
    pass


class UploadIncomplete(ValueError):
    pass


def intake_store() -> ObjectStore:
    return object_store("intake")


def _segment(value: str) -> str:
    cleaned = _SEGMENT_RE.sub("_", str(value or "")).strip("._")
    return cleaned or "default"


def clean_name(name: str) -> str:
    cleaned = _NAME_RE.sub("_", str(name or "")).strip()
    return cleaned[:200] or "attachment"


def tenant_prefix(tenant_id: str) -> str:
    return f"{INTAKE_PREFIX}{_segment(tenant_id)}/"


def upload_key(*, tenant_id: str, sha256: str) -> str:
    return check_key(f"{tenant_prefix(tenant_id)}{sha256[:2]}/{sha256}")


def discord_key(*, tenant_id: str, attachment_id: str) -> str:
    return check_key(f"{tenant_prefix(tenant_id)}discord/{_segment(attachment_id)}")


class IntakeSpool:
    """Collects an upload chunk by chunk (memory, then disk past 1 MB) while hashing it.

    Feed it from a sync file (``stash_stream``) or an async request stream, then ``store`` it.
    """

    def __init__(self, *, max_bytes: int = MAX_FILE_BYTES) -> None:
        self.max_bytes = max_bytes
        self.size = 0
        self._digest = hashlib.sha256()
        self._file = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise UploadTooLarge(f"file is larger than {self.max_bytes // (1024 * 1024)} MB")
        self._digest.update(chunk)
        self._file.write(chunk)

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "IntakeSpool":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def store(
        self,
        *,
        tenant_id: str,
        name: str,
        mime: str | None,
        key: str | None = None,
        store: ObjectStore | None = None,
    ) -> dict[str, Any]:
        """Write the spooled bytes (skipped when an identical object exists); return a ``stored`` pointer.

        Without ``key`` the object is content-addressed, so an identical earlier upload is reused.
        """
        store = store or intake_store()
        sha = self._digest.hexdigest()
        object_key = key or upload_key(tenant_id=tenant_id, sha256=sha)
        existing = store.head(object_key)
        if existing is None or existing.sha256 != sha or existing.size != self.size:
            self._file.seek(0)
            store.put_file(object_key, self._file, size=self.size, sha256=sha, content_type=guess_mime(name, mime))
        return {
            "kind": "stored",
            "object_key": object_key,
            "sha256": sha,
            "size": self.size,
            "name": clean_name(name),
            "mime": guess_mime(name, mime),
        }


def stash_stream(
    *,
    tenant_id: str,
    name: str,
    mime: str | None,
    fileobj: BinaryIO,
    size: int,
    key: str | None = None,
    store: ObjectStore | None = None,
    max_bytes: int = MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Read exactly ``size`` bytes from ``fileobj`` into the store; return a ``stored`` pointer."""
    if size < 0:
        raise UploadIncomplete("missing size")
    if size > max_bytes:
        raise UploadTooLarge(f"file is larger than {max_bytes // (1024 * 1024)} MB")
    with IntakeSpool(max_bytes=max_bytes) as spool:
        while spool.size < size:
            chunk = fileobj.read(min(_CHUNK, size - spool.size))
            if not chunk:
                break
            spool.write(chunk)
        if spool.size != size:
            raise UploadIncomplete(f"expected {size} bytes, received {spool.size}")
        return spool.store(tenant_id=tenant_id, name=name, mime=mime, key=key, store=store)


def load_bytes(pointer: dict[str, Any], *, store: ObjectStore | None = None) -> bytes:
    """The object's bytes, checked against the pointer's sha256. Raises ``ObjectNotFound`` once expired."""
    store = store or intake_store()
    return store.get(str(pointer.get("object_key") or ""), expected_sha256=pointer.get("sha256") or None)


def validate_stored_pointer(
    pointer: dict[str, Any], *, tenant_id: str, store: ObjectStore | None = None
) -> dict[str, Any]:
    """A client-supplied ``stored`` pointer, checked against the store.

    The key must sit under the caller's tenant, and ``size``/``sha256`` must match the object,
    so a client can neither read another tenant's upload nor understate a size to slip the caps.
    """
    name = clean_name(str(pointer.get("name") or "attachment"))
    out: dict[str, Any] = {"kind": "stored", "name": name, "mime": guess_mime(name, str(pointer.get("mime") or ""))}
    key = str(pointer.get("object_key") or "")
    sha = str(pointer.get("sha256") or "")
    try:
        size = int(pointer.get("size"))
    except (TypeError, ValueError):
        size = -1
    try:
        check_key(key)
        valid_key = key.startswith(tenant_prefix(tenant_id))
    except ValueError:
        valid_key = False
    info = None
    if valid_key and sha and size >= 0:
        store = store or intake_store()
        info = store.head(key)
    if info is None or info.size != size or info.sha256 != sha:
        out["error"] = "attachment not found"
        return out
    out.update({"object_key": key, "sha256": sha, "size": size})
    return out


def sweep_local_intake(
    store: ObjectStore, *, older_than_days: float = INTAKE_RETENTION_DAYS, now: float | None = None
) -> int:
    """Delete local intake objects older than the retention window. Cloud stores use lifecycle rules."""
    if not isinstance(store, LocalObjectStore):
        return 0
    cutoff = (now if now is not None else time.time()) - older_than_days * 86400
    removed = 0
    for item in store.list(INTAKE_PREFIX):
        path = store._path(item.key)  # noqa: SLF001 - the local store owns its layout
        try:
            if path.stat().st_mtime < cutoff:
                store.delete(item.key)
                removed += 1
        except FileNotFoundError:
            continue
    return removed
