"""Object storage for skill files and agent workspaces: a local directory, S3 or GCS.

Every object carries its sha256 in metadata, and reads are checked against it,
so a corrupted or swapped object is refused instead of served.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ts_harness.storage.backend import StorageConfigError, object_backend

SHA_METADATA_KEY = "sha256"
DEFAULT_LOCAL_OBJECT_DIR = "/var/lib/ts-harness/objects"
_KEY_RE = re.compile(r"^[A-Za-z0-9!_.*'()/=+@,-]+$")


class ObjectNotFound(KeyError):
    pass


class ChecksumMismatch(ValueError):
    """The object's bytes don't match the sha256 it was stored with (or the one the caller expected)."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    sha256: str | None = None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_key(key: str) -> str:
    parts = key.split("/")
    if not key or key.startswith("/") or not _KEY_RE.fullmatch(key) or any(p in {"", ".", ".."} for p in parts):
        raise ValueError(f"invalid object key: {key!r}")
    return key


def check_prefix(prefix: str) -> str:
    if prefix and not prefix.endswith("/"):
        raise ValueError(f"object prefix must end with '/': {prefix!r}")
    if prefix:
        check_key(prefix.rstrip("/"))
    return prefix


def _verify(key: str, data: bytes, stored: str | None, expected: str | None) -> bytes:
    actual = sha256_hex(data)
    for label, want in (("stored", stored), ("expected", expected)):
        if want and want != actual:
            raise ChecksumMismatch(f"{key}: sha256 {actual[:12]} does not match the {label} {want[:12]}")
    return data


class ObjectStore(ABC):
    @abstractmethod
    def get(self, key: str, *, expected_sha256: str | None = None) -> bytes: ...

    @abstractmethod
    def put(self, key: str, data: bytes) -> ObjectInfo: ...

    @abstractmethod
    def copy(self, source: str, dest: str) -> ObjectInfo: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def list(self, prefix: str) -> list[ObjectInfo]: ...

    @abstractmethod
    def head(self, key: str) -> ObjectInfo | None: ...

    def delete_prefix(self, prefix: str) -> int:
        check_prefix(prefix)
        if not prefix:
            raise ValueError("refusing to delete every object")
        items = self.list(prefix)
        for item in items:
            self.delete(item.key)
        return len(items)


class LocalObjectStore(ObjectStore):
    """A directory; the sha256 of each object sits in a hidden sidecar next to it."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def _meta(self, key: str) -> Path:
        return self.root / ".meta" / f"{check_key(key)}.sha256"

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".obj-", delete=False) as handle:
            handle.write(data)
            tmp = Path(handle.name)
        tmp.replace(path)

    def get(self, key: str, *, expected_sha256: str | None = None) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFound(key)
        meta = self._meta(key)
        stored = meta.read_text().strip() if meta.is_file() else None
        return _verify(key, path.read_bytes(), stored, expected_sha256)

    def put(self, key: str, data: bytes) -> ObjectInfo:
        sha = sha256_hex(data)
        self._write(self._meta(key), sha.encode())
        self._write(self._path(key), data)
        return ObjectInfo(key, len(data), sha)

    def copy(self, source: str, dest: str) -> ObjectInfo:
        return self.put(dest, self.get(source))

    def delete(self, key: str) -> None:
        for path in (self._path(key), self._meta(key)):
            path.unlink(missing_ok=True)

    def list(self, prefix: str) -> list[ObjectInfo]:
        check_prefix(prefix)
        base = self.root / prefix if prefix else self.root
        if not base.is_dir():
            return []
        out = []
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(self.root)
            if path.is_file() and not any(part.startswith(".") for part in rel.parts):
                meta = self._meta(rel.as_posix())
                out.append(ObjectInfo(rel.as_posix(), path.stat().st_size, meta.read_text().strip() if meta.is_file() else None))
        return out

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        meta = self._meta(key)
        return ObjectInfo(key, path.stat().st_size, meta.read_text().strip() if meta.is_file() else None)


class S3ObjectStore(ObjectStore):
    def __init__(self, bucket: str, client: Any = None) -> None:
        self.bucket = bucket
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    @staticmethod
    def _missing(exc: Exception) -> bool:
        response = getattr(exc, "response", None) or {}
        code = str((response.get("Error") or {}).get("Code") or "")
        return code in {"404", "NoSuchKey", "NotFound"}

    def get(self, key: str, *, expected_sha256: str | None = None) -> bytes:
        try:
            out = self.client.get_object(Bucket=self.bucket, Key=check_key(key))
        except Exception as exc:
            if self._missing(exc):
                raise ObjectNotFound(key) from exc
            raise
        stored = (out.get("Metadata") or {}).get(SHA_METADATA_KEY)
        return _verify(key, out["Body"].read(), stored, expected_sha256)

    def put(self, key: str, data: bytes) -> ObjectInfo:
        sha = sha256_hex(data)
        self.client.put_object(Bucket=self.bucket, Key=check_key(key), Body=data, Metadata={SHA_METADATA_KEY: sha})
        return ObjectInfo(key, len(data), sha)

    def copy(self, source: str, dest: str) -> ObjectInfo:
        info = self.head(source)
        if info is None:
            raise ObjectNotFound(source)
        self.client.copy_object(
            Bucket=self.bucket,
            Key=check_key(dest),
            CopySource={"Bucket": self.bucket, "Key": check_key(source)},
            MetadataDirective="COPY",
        )
        return ObjectInfo(dest, info.size, info.sha256)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=check_key(key))

    def delete_prefix(self, prefix: str) -> int:
        check_prefix(prefix)
        if not prefix:
            raise ValueError("refusing to delete every object")
        keys = [item.key for item in self.list(prefix)]
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True}
            )
        return len(keys)

    def list(self, prefix: str) -> list[ObjectInfo]:
        check_prefix(prefix)
        out: list[ObjectInfo] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            out.extend(ObjectInfo(str(item["Key"]), int(item.get("Size") or 0)) for item in page.get("Contents") or [])
            if not page.get("IsTruncated"):
                return out
            token = page.get("NextContinuationToken")

    def head(self, key: str) -> ObjectInfo | None:
        try:
            out = self.client.head_object(Bucket=self.bucket, Key=check_key(key))
        except Exception as exc:
            if self._missing(exc):
                return None
            raise
        return ObjectInfo(key, int(out.get("ContentLength") or 0), (out.get("Metadata") or {}).get(SHA_METADATA_KEY))


class GcsObjectStore(ObjectStore):
    """Google Cloud Storage; needs the ``gcp`` extra (google-cloud-storage)."""

    def __init__(self, bucket: str, client: Any = None) -> None:
        self.bucket_name = bucket
        self._client = client

    @property
    def bucket(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise StorageConfigError("TS_HARNESS_OBJECT_BACKEND=gcs needs the gcp extra (google-cloud-storage)") from exc
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def get(self, key: str, *, expected_sha256: str | None = None) -> bytes:
        blob = self.bucket.get_blob(check_key(key))
        if blob is None:
            raise ObjectNotFound(key)
        return _verify(key, blob.download_as_bytes(), (blob.metadata or {}).get(SHA_METADATA_KEY), expected_sha256)

    def put(self, key: str, data: bytes) -> ObjectInfo:
        sha = sha256_hex(data)
        blob = self.bucket.blob(check_key(key))
        blob.metadata = {SHA_METADATA_KEY: sha}
        blob.upload_from_string(data)
        return ObjectInfo(key, len(data), sha)

    def copy(self, source: str, dest: str) -> ObjectInfo:
        bucket = self.bucket
        blob = bucket.get_blob(check_key(source))
        if blob is None:
            raise ObjectNotFound(source)
        bucket.copy_blob(blob, bucket, check_key(dest))
        return ObjectInfo(dest, int(blob.size or 0), (blob.metadata or {}).get(SHA_METADATA_KEY))

    def delete(self, key: str) -> None:
        blob = self.bucket.get_blob(check_key(key))
        if blob is not None:
            blob.delete()

    def list(self, prefix: str) -> list[ObjectInfo]:
        check_prefix(prefix)
        return [
            ObjectInfo(blob.name, int(blob.size or 0), (blob.metadata or {}).get(SHA_METADATA_KEY))
            for blob in self.bucket.client.list_blobs(self.bucket_name, prefix=prefix)
        ]

    def head(self, key: str) -> ObjectInfo | None:
        blob = self.bucket.get_blob(check_key(key))
        if blob is None:
            return None
        return ObjectInfo(key, int(blob.size or 0), (blob.metadata or {}).get(SHA_METADATA_KEY))


_BUCKET_ENV = {"skills": "TS_HARNESS_SKILLS_BUCKET", "workspaces": "TS_HARNESS_WORKSPACE_BUCKET"}
_STORES: dict[tuple[str, str, str], ObjectStore] = {}


def object_store(kind: str) -> ObjectStore:
    """The store for ``skills`` or ``workspaces`` under the configured backend."""
    if kind not in _BUCKET_ENV:
        raise ValueError(f"unknown object store kind: {kind}")
    backend = object_backend()
    if backend == "local":
        location = str(Path(os.environ.get("TS_HARNESS_LOCAL_OBJECT_DIR") or DEFAULT_LOCAL_OBJECT_DIR) / kind)
    else:
        location = (os.environ.get(_BUCKET_ENV[kind]) or "").strip()
        if not location:
            raise StorageConfigError(f"TS_HARNESS_OBJECT_BACKEND={backend} needs {_BUCKET_ENV[kind]}")
    key = (kind, backend, location)
    store = _STORES.get(key)
    if store is None:
        store = (
            LocalObjectStore(location) if backend == "local"
            else S3ObjectStore(location) if backend == "s3"
            else GcsObjectStore(location)
        )
        _STORES[key] = store
    return store
