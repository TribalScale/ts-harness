"""Turn intake pointers into capped text records.

Fetching is injected (``download``, ``drive``, ``describe_image``) so this module is pure
logic: caps, budgets, folder expansion, and error records. ``connectors`` holds the real
clients. A bad source never raises; it becomes a record with ``error`` set.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Protocol

from ts_harness.intake.sources import (
    MAX_FILE_BYTES,
    MAX_FOLDER_CHILDREN,
    MAX_POINTERS,
    MAX_TEXT_CHARS,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_TEXT_CHARS,
    clip_text,
    dedupe_pointers,
    extract_document,
)
from ts_harness.storage.objects import ObjectNotFound

logger = logging.getLogger(__name__)

EXPIRED = "attachment expired; please attach it again"
NOT_SHARED = "not shared with the workspace account"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_DRIVE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", "text/plain"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "text/csv"),
    "application/vnd.google-apps.presentation": ("text/plain", "text/plain"),
}


class SourceUnavailable(Exception):
    """A source that cannot be read; the message is shown to the requester."""


class DriveAccessError(SourceUnavailable):
    def __init__(self, message: str = NOT_SHARED) -> None:
        super().__init__(message)


class DriveSource(Protocol):
    def metadata(self, file_id: str) -> dict[str, Any]: ...

    def export(self, file_id: str, mime: str) -> bytes: ...

    def download(self, file_id: str) -> bytes: ...

    def list_children(self, folder_id: str, limit: int) -> list[dict[str, Any]]: ...


Download = Callable[[dict[str, Any]], tuple[str, str, bytes]]
DescribeImage = Callable[[str, str, bytes], str]


class _Budget:
    def __init__(self, *, deadline_s: float, max_total_text: int, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.deadline = clock() + deadline_s
        self.max_total_text = max_total_text
        self.bytes_used = 0
        self.text_used = 0

    def out_of_time(self) -> bool:
        return self.clock() >= self.deadline

    def text_left(self) -> int:
        return max(0, self.max_total_text - self.text_used)

    def stop_reason(self) -> str | None:
        if self.out_of_time():
            return "time budget reached"
        if self.text_left() <= 0:
            return "text budget reached"
        return None


def _record(kind: str, name: str, *, url: str = "", mime: str = "", text: str = "", truncated: bool = False,
            error: str | None = None) -> dict[str, Any]:
    return {"kind": kind, "name": name, "url": url, "mime": mime, "text": text, "truncated": truncated, "error": error}


def _pointer_name(pointer: dict[str, Any]) -> str:
    return str(pointer.get("name") or pointer.get("url") or pointer.get("file_id") or "attachment")


def _check_bytes(budget: _Budget, size: int) -> str | None:
    """``error`` for a file over the per-file cap, ``skip`` reason past the total, else ``None``."""
    if size > MAX_FILE_BYTES:
        return "error:file is larger than 20 MB"
    if budget.bytes_used + size > MAX_TOTAL_BYTES:
        return "skip:byte budget reached"
    return None


def _text_record(
    record: dict[str, Any], name: str, mime: str, data: bytes, budget: _Budget, describe_image: DescribeImage | None
) -> dict[str, Any]:
    limit = min(MAX_TEXT_CHARS, budget.text_left())
    extracted = extract_document(name, mime, data, limit=limit)
    if extracted["needs_vision"]:
        if describe_image is None:
            record["error"] = "image reading is not enabled"
            return record
        try:
            text, truncated = clip_text(describe_image(name, mime, data).strip(), limit)
        except SourceUnavailable as exc:
            record["error"] = str(exc)
            return record
        except Exception as exc:  # noqa: BLE001 - provider failures become an error record
            logger.warning("intake image description failed for %s: %s", name, exc)
            record["error"] = "image could not be read"
            return record
        record.update(text=text, truncated=truncated)
    elif extracted["error"]:
        record["error"] = extracted["error"]
        return record
    else:
        record.update(text=extracted["text"], truncated=extracted["truncated"])
    budget.text_used += len(record["text"])
    return record


def _drive_file(
    meta: dict[str, Any], drive: DriveSource, budget: _Budget, describe_image: DescribeImage | None, *, name: str, url: str
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """One Drive file: (record, None) or (None, skipped entry)."""
    mime = str(meta.get("mimeType") or "")
    record = _record("drive", name, url=url, mime=mime)
    file_id = str(meta.get("id") or "")
    if mime in _DRIVE_EXPORTS:
        export_mime, text_mime = _DRIVE_EXPORTS[mime]
        data = drive.export(file_id, export_mime)
        verdict = _check_bytes(budget, len(data))
        if verdict and verdict.startswith("skip:"):
            return None, {"name": name, "reason": verdict[5:]}
        budget.bytes_used += len(data)
        return _text_record(record, name, text_mime, data, budget, describe_image), None
    if mime.startswith("application/vnd.google-apps."):
        record["error"] = "unsupported Google file type"
        return record, None
    try:
        size = int(meta.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    verdict = _check_bytes(budget, size)
    if verdict and verdict.startswith("error:"):
        record["error"] = verdict[6:]
        return record, None
    if verdict:
        return None, {"name": name, "reason": verdict[5:]}
    data = drive.download(file_id)
    if len(data) > MAX_FILE_BYTES:
        record["error"] = "file is larger than 20 MB"
        return record, None
    # Charge what was planned against (metadata) or what arrived, whichever is larger.
    budget.bytes_used += max(size, len(data))
    return _text_record(record, name, mime, data, budget, describe_image), None


def _resolve_drive(
    pointer: dict[str, Any], drive: DriveSource | None, budget: _Budget, describe_image: DescribeImage | None,
    sources: list[dict[str, Any]], skipped: list[dict[str, str]],
) -> None:
    url = str(pointer.get("url") or "")
    if drive is None:
        sources.append(_record("drive", _pointer_name(pointer), url=url, error="Google Drive is not connected"))
        return
    meta = drive.metadata(str(pointer.get("file_id") or ""))
    name = str(meta.get("name") or _pointer_name(pointer))
    if str(meta.get("mimeType") or "") != DRIVE_FOLDER_MIME:
        record, skip = _drive_file(meta, drive, budget, describe_image, name=name, url=url)
        if record is not None:
            sources.append(record)
        if skip is not None:
            skipped.append(skip)
        return
    children = drive.list_children(str(meta.get("id") or pointer.get("file_id")), MAX_FOLDER_CHILDREN + 1)
    if len(children) > MAX_FOLDER_CHILDREN:
        skipped.append({"name": name, "reason": f"folder has more than {MAX_FOLDER_CHILDREN} files; read the first {MAX_FOLDER_CHILDREN}"})
    files = [child for child in children[:MAX_FOLDER_CHILDREN] if child.get("mimeType") != DRIVE_FOLDER_MIME]
    for child in children[:MAX_FOLDER_CHILDREN]:
        if child.get("mimeType") == DRIVE_FOLDER_MIME:
            skipped.append({"name": f"{name}/{child.get('name')}", "reason": "subfolders are not read"})
    if not files:
        sources.append(_record("drive", name, url=url, mime=DRIVE_FOLDER_MIME, error="folder has no readable files"))
        return
    for child in files:
        child_name = f"{name}/{child.get('name') or child.get('id')}"
        reason = budget.stop_reason()
        if reason:
            skipped.append({"name": child_name, "reason": reason})
            continue
        child_url = str(child.get("webViewLink") or url)
        try:
            record, skip = _drive_file(child, drive, budget, describe_image, name=child_name, url=child_url)
        except SourceUnavailable as exc:
            record, skip = _record("drive", child_name, url=child_url, error=str(exc)), None
        except Exception as exc:  # noqa: BLE001
            logger.warning("intake drive child %s failed: %s", child.get("id"), exc)
            record, skip = _record("drive", child_name, url=child_url, error="file could not be read"), None
        if record is not None:
            sources.append(record)
        if skip is not None:
            skipped.append(skip)


def _resolve_file(
    pointer: dict[str, Any], download: Download, budget: _Budget, describe_image: DescribeImage | None,
    sources: list[dict[str, Any]], skipped: list[dict[str, str]],
) -> None:
    kind = str(pointer.get("kind") or "")
    name = _pointer_name(pointer)
    url = str(pointer.get("url") or "") if kind == "discord_file" else ""
    try:
        declared = int(pointer.get("size") or 0)
    except (TypeError, ValueError):
        declared = 0
    verdict = _check_bytes(budget, declared)
    if verdict and verdict.startswith("error:"):
        sources.append(_record(kind, name, url=url, mime=str(pointer.get("mime") or ""), error=verdict[6:]))
        return
    if verdict:
        skipped.append({"name": name, "reason": verdict[5:]})
        return
    fetched_name, mime, data = download(pointer)
    name = fetched_name or name
    record = _record(kind, name, url=url, mime=mime)
    if len(data) > MAX_FILE_BYTES:
        record["error"] = "file is larger than 20 MB"
        sources.append(record)
        return
    if budget.bytes_used + len(data) > MAX_TOTAL_BYTES:
        skipped.append({"name": name, "reason": "byte budget reached"})
        return
    budget.bytes_used += len(data)
    sources.append(_text_record(record, name, mime, data, budget, describe_image))


def resolve_intake_sources(
    pointers: list[dict[str, Any]],
    *,
    download: Download,
    drive: DriveSource | None,
    describe_image: DescribeImage | None,
    deadline_s: float = 120,
    max_total_text: int = MAX_TOTAL_TEXT_CHARS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """``{sources, sources_text, skipped}`` for ``pointers``. Raises only when ``pointers`` is not a list."""
    if not isinstance(pointers, list):
        raise ValueError("intake_sources must be a list")
    budget = _Budget(deadline_s=deadline_s, max_total_text=max_total_text, clock=clock)
    sources: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    unique = dedupe_pointers([p for p in pointers if isinstance(p, dict)])
    for index, pointer in enumerate(unique):
        kind = str(pointer.get("kind") or "")
        name = _pointer_name(pointer)
        if index >= MAX_POINTERS:
            skipped.append({"name": name, "reason": f"more than {MAX_POINTERS} attachments"})
            continue
        if pointer.get("error"):
            sources.append(_record(kind, name, url=str(pointer.get("url") or ""), mime=str(pointer.get("mime") or ""),
                                   error=str(pointer["error"])))
            continue
        reason = budget.stop_reason()
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        try:
            if kind == "drive":
                _resolve_drive(pointer, drive, budget, describe_image, sources, skipped)
            else:
                _resolve_file(pointer, download, budget, describe_image, sources, skipped)
        except ObjectNotFound:
            sources.append(_record(kind, name, error=EXPIRED))
        except SourceUnavailable as exc:
            sources.append(_record(kind, name, url=str(pointer.get("url") or ""), error=str(exc)))
        except Exception as exc:  # noqa: BLE001 - one bad source never fails the gather
            logger.warning("intake source %s (%s) failed: %s", name, kind, exc)
            sources.append(_record(kind, name, url=str(pointer.get("url") or ""), error="file could not be read"))
    return {"sources": sources, "sources_text": render_sources_text(sources, skipped), "skipped": skipped}


def render_sources_text(sources: list[dict[str, Any]], skipped: list[dict[str, str]]) -> str:
    """Markdown for prompts: one section per source, then what was left out and why."""
    if not sources and not skipped:
        return ""
    lines = ["## Shared sources", ""]
    for number, source in enumerate(sources, start=1):
        title = f"### {number}. {source['name']}"
        if source.get("url"):
            title += f" ({source['url']})"
        lines.append(title)
        if source.get("error"):
            lines.append(f"_Could not be read: {source['error']}._")
        else:
            if source.get("truncated"):
                lines.append("_Cut at the size limit; later content is missing._")
            lines.append(source.get("text") or "_(empty)_")
        lines.append("")
    if skipped:
        lines.append("### Not read")
        lines.extend(f"- {item['name']}: {item['reason']}" for item in skipped)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
