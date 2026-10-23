"""In-memory S3 and GCS clients with just the calls ObjectStore makes."""

from __future__ import annotations

import io
from typing import Any


class _ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.page_size = page_size
        self.calls: list[str] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, Metadata: dict[str, str]) -> dict:
        self.calls.append("put_object")
        self.objects[(Bucket, Key)] = (bytes(Body), dict(Metadata))
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        self.calls.append("get_object")
        if (Bucket, Key) not in self.objects:
            raise _ClientError("NoSuchKey")
        data, meta = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(data), "Metadata": dict(meta), "ContentLength": len(data)}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        self.calls.append("head_object")
        if (Bucket, Key) not in self.objects:
            raise _ClientError("404")
        data, meta = self.objects[(Bucket, Key)]
        return {"Metadata": dict(meta), "ContentLength": len(data)}

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict[str, str], MetadataDirective: str) -> dict:
        self.calls.append("copy_object")
        assert MetadataDirective == "COPY"
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:
        self.calls.append("delete_object")
        self.objects.pop((Bucket, Key), None)
        return {}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict:
        self.calls.append("delete_objects")
        assert len(Delete["Objects"]) <= 1000
        for item in Delete["Objects"]:
            self.objects.pop((Bucket, item["Key"]), None)
        return {}

    def list_objects_v2(self, *, Bucket: str, Prefix: str, ContinuationToken: str | None = None) -> dict:
        self.calls.append("list_objects_v2")
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        out: dict[str, Any] = {
            "Contents": [{"Key": k, "Size": len(self.objects[(Bucket, k)][0])} for k in page],
            "IsTruncated": more,
        }
        if more:
            out["NextContinuationToken"] = str(start + self.page_size)
        return out


class _Blob:
    def __init__(self, bucket: "_Bucket", name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None
        self.data = b""

    @property
    def size(self) -> int:
        return len(self.data)

    def upload_from_string(self, data: bytes) -> None:
        self.data = bytes(data)
        self._bucket.blobs[self.name] = self

    def download_as_bytes(self) -> bytes:
        return self.data

    def delete(self) -> None:
        self._bucket.blobs.pop(self.name, None)


class _Bucket:
    def __init__(self, client: "FakeGcs", name: str) -> None:
        self.client = client
        self.name = name
        self.blobs: dict[str, _Blob] = {}

    def blob(self, name: str) -> _Blob:
        return _Blob(self, name)

    def get_blob(self, name: str) -> _Blob | None:
        return self.blobs.get(name)

    def copy_blob(self, blob: _Blob, dest_bucket: "_Bucket", new_name: str) -> _Blob:
        copy = _Blob(dest_bucket, new_name)
        copy.metadata = dict(blob.metadata or {})
        copy.upload_from_string(blob.data)
        return copy


class FakeGcs:
    def __init__(self) -> None:
        self.buckets: dict[str, _Bucket] = {}

    def bucket(self, name: str) -> _Bucket:
        return self.buckets.setdefault(name, _Bucket(self, name))

    def list_blobs(self, bucket: str, prefix: str = "") -> list[_Blob]:
        return [b for n, b in sorted(self.bucket(bucket).blobs.items()) if n.startswith(prefix)]
