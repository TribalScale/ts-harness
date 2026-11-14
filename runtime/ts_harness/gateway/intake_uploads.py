"""``POST /intake/uploads``: one file per request, streamed into the intake object store.

The body is the raw file (not JSON, not multipart); ``Content-Type`` is its MIME type and
``?name=`` its file name. The response is a ``stored`` pointer the client then sends in
``attachments`` on ``/session/message``.

Both HTTP servers use this module: the Starlette app feeds an ``IntakeSpool`` from the async
request stream, the threaded fallback reads ``rfile``.
"""

from __future__ import annotations

import os
from typing import Any, BinaryIO, NamedTuple

from ts_harness.intake.blobs import IntakeSpool, UploadIncomplete, UploadTooLarge, clean_name, stash_stream
from ts_harness.intake.sources import MAX_FILE_BYTES

UPLOAD_PATH = "/intake/uploads"
DEFAULT_MAX_JSON_BODY_BYTES = 10 * 1024 * 1024


class UploadRejected(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class BodyTooLarge(Exception):
    pass


class UploadRequest(NamedTuple):
    name: str
    mime: str
    size: int


def max_json_body_bytes() -> int:
    raw = (os.environ.get("TS_HARNESS_MAX_JSON_BODY_BYTES") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_JSON_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_JSON_BODY_BYTES


def json_body_too_large(path: str, content_length: str | None) -> bool:
    """True when a declared body past the JSON ceiling targets anything but the upload route."""
    if path == UPLOAD_PATH or not content_length:
        return False
    try:
        return int(content_length) > max_json_body_bytes()
    except ValueError:
        return False


def upload_request(name: str | None, content_type: str | None, content_length: str | None) -> UploadRequest:
    """Validate the upload's headers before any of the body is read."""
    if content_length is None or not str(content_length).strip():
        raise UploadRejected(411, "Content-Length is required")
    try:
        size = int(content_length)
    except ValueError as exc:
        raise UploadRejected(400, "invalid Content-Length") from exc
    if size <= 0:
        raise UploadRejected(400, "the file is empty")
    if size > MAX_FILE_BYTES:
        raise UploadRejected(413, f"files are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB each")
    mime = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    return UploadRequest(name=clean_name(name or "attachment"), mime=mime, size=size)


def store_spool(spool: IntakeSpool, request: UploadRequest, *, tenant_id: str) -> dict[str, Any]:
    if spool.size != request.size:
        raise UploadRejected(400, f"expected {request.size} bytes, received {spool.size}")
    return spool.store(tenant_id=tenant_id, name=request.name, mime=request.mime)


def store_from_file(fileobj: BinaryIO, request: UploadRequest, *, tenant_id: str) -> dict[str, Any]:
    try:
        return stash_stream(
            tenant_id=tenant_id, name=request.name, mime=request.mime, fileobj=fileobj, size=request.size
        )
    except UploadTooLarge as exc:
        raise UploadRejected(413, str(exc)) from exc
    except UploadIncomplete as exc:
        raise UploadRejected(400, str(exc)) from exc
