"""Intake pointers, their caps, and text extraction from document bytes.

A pointer is a small dict naming where a source lives, never its bytes:

- ``slack_file``: ``file_id``, ``name``, ``mime``, ``size``
- ``telegram_file``: ``file_id``, ``file_unique_id``, ``name``, ``mime``, ``size``
- ``discord_file``: ``attachment_id``, ``url``, ``name``, ``mime``, ``size``, ``object_key``
- ``stored``: ``object_key``, ``sha256``, ``size``, ``name``, ``mime`` (console uploads)
- ``drive``: ``url``, ``file_id``, ``drive_type``

Any pointer may carry ``error``; it is then reported at gather, not fetched.
"""

from __future__ import annotations

import io
import mimetypes
import re
import zipfile
from typing import Any, TypedDict
from xml.etree import ElementTree

MAX_POINTERS = 25
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024
MAX_TEXT_CHARS = 80_000
MAX_TOTAL_TEXT_CHARS = 150_000
MAX_FOLDER_CHILDREN = 50
INTAKE_RETENTION_DAYS = 14

POINTER_KINDS = frozenset({"slack_file", "telegram_file", "discord_file", "stored", "drive"})

# A DOCX's document.xml past this size is refused rather than inflated (zip bombs).
_MAX_DOCX_XML_BYTES = 50 * 1024 * 1024

_DRIVE_ID = r"([A-Za-z0-9_-]{10,})"
_DRIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://docs\.google\.com/document/(?:u/\d+/)?d/" + _DRIVE_ID), "document"),
    (re.compile(r"https?://docs\.google\.com/spreadsheets/(?:u/\d+/)?d/" + _DRIVE_ID), "spreadsheet"),
    (re.compile(r"https?://docs\.google\.com/presentation/(?:u/\d+/)?d/" + _DRIVE_ID), "presentation"),
    (re.compile(r"https?://drive\.google\.com/file/(?:u/\d+/)?d/" + _DRIVE_ID), "file"),
    (re.compile(r"https?://drive\.google\.com/drive/(?:u/\d+/)?folders/" + _DRIVE_ID), "folder"),
    (re.compile(r"https?://drive\.google\.com/open\?(?:[^\s#]*&)?id=" + _DRIVE_ID), "unknown"),
)

_TEXT_MIMES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/csv",
    }
)
_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".log"})
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"
VISION_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ExtractResult(TypedDict):
    text: str
    truncated: bool
    error: str | None
    needs_vision: bool


def _result(text: str = "", *, truncated: bool = False, error: str | None = None, needs_vision: bool = False) -> ExtractResult:
    return {"text": text, "truncated": truncated, "error": error, "needs_vision": needs_vision}


def clip_text(text: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


# ---------------------------------------------------------------------------
# Pointers
# ---------------------------------------------------------------------------


def parse_drive_urls(text: str) -> list[dict[str, Any]]:
    """Drive pointers for every Google Docs/Sheets/Slides/Drive file or folder link in ``text``."""
    found: list[tuple[int, dict[str, Any]]] = []
    for pattern, drive_type in _DRIVE_PATTERNS:
        for match in pattern.finditer(text or ""):
            found.append(
                (
                    match.start(),
                    {"kind": "drive", "url": match.group(0), "file_id": match.group(1), "drive_type": drive_type},
                )
            )
    found.sort(key=lambda item: item[0])
    return dedupe_pointers([pointer for _start, pointer in found])


def pointer_identity(pointer: dict[str, Any]) -> str:
    kind = str(pointer.get("kind") or "")
    key = {
        "drive": pointer.get("file_id") or pointer.get("url"),
        "slack_file": pointer.get("file_id"),
        "telegram_file": pointer.get("file_unique_id") or pointer.get("file_id"),
        "discord_file": pointer.get("attachment_id") or pointer.get("url"),
        "stored": pointer.get("sha256") or pointer.get("object_key"),
    }.get(kind)
    if key:
        return f"{kind}:{key}"
    # No id (e.g. a refused attachment): distinct names and errors stay distinct entries.
    return f"{kind}:?:{pointer.get('name')}:{pointer.get('url')}:{pointer.get('error')}"


def dedupe_pointers(pointers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for pointer in pointers:
        identity = pointer_identity(pointer)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(pointer)
    return out


def pointers_from_message(*, text: str, attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Adapter-built attachment pointers first, then Drive links in the text; deduped.

    Entries that are not dicts or name an unknown kind are dropped. An entry carrying raw
    file content is kept as an error pointer so the requester is told to upload instead.
    """
    out: list[dict[str, Any]] = []
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        if "content_base64" in item:
            out.append(
                {
                    "kind": "stored",
                    "name": str(item.get("name") or "attachment"),
                    "mime": str(item.get("mime") or ""),
                    "error": "upload the file through /intake/uploads",
                }
            )
            continue
        if item.get("kind") in POINTER_KINDS:
            out.append(dict(item))
    out.extend(parse_drive_urls(text))
    return dedupe_pointers(out)


def _size(pointer: dict[str, Any]) -> int:
    try:
        return max(0, int(pointer.get("size") or 0))
    except (TypeError, ValueError):
        return 0


def apply_arrival_caps(
    pointers: list[dict[str, Any]], *, existing: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Mark new pointers that break the count, per-file, or running-total caps with ``error``.

    ``existing`` are pointers already accepted for the same program; they count toward the
    caps but are not returned. Duplicates of existing pointers are dropped.
    """
    prior = [p for p in existing or [] if not p.get("error")]
    seen = {pointer_identity(p) for p in existing or []}
    count = len(prior)
    total = sum(_size(p) for p in prior)
    out: list[dict[str, Any]] = []
    for pointer in pointers:
        identity = pointer_identity(pointer)
        if identity in seen:
            continue
        seen.add(identity)
        pointer = dict(pointer)
        if pointer.get("error"):
            out.append(pointer)
            continue
        size = _size(pointer)
        if count >= MAX_POINTERS:
            pointer["error"] = f"too many attachments (limit {MAX_POINTERS})"
        elif size > MAX_FILE_BYTES:
            pointer["error"] = f"file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
        elif total + size > MAX_TOTAL_BYTES:
            pointer["error"] = f"attachments exceed {MAX_TOTAL_BYTES // (1024 * 1024)} MB in total"
        else:
            count += 1
            total += size
        out.append(pointer)
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def guess_mime(name: str, mime: str | None) -> str:
    value = (mime or "").split(";", 1)[0].strip().lower()
    if value and value != "application/octet-stream":
        return value
    guessed, _ = mimetypes.guess_type(name or "")
    return (guessed or value or "application/octet-stream").lower()


def _suffix(name: str) -> str:
    dot = (name or "").rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def _is_text(name: str, mime: str) -> bool:
    return mime.startswith("text/") or mime in _TEXT_MIMES or mime.endswith("+json") or _suffix(name) in _TEXT_SUFFIXES


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > _MAX_DOCX_XML_BYTES:
            raise ValueError("document body is too large")
        root = ElementTree.fromstring(archive.read(info))
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_W_NS}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W_NS}tab":
                parts.append("\t")
            elif node.tag in {f"{_W_NS}br", f"{_W_NS}cr"}:
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs).strip()


def _pdf_text(data: bytes, limit: int) -> tuple[str, bool]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError("encrypted")
        except Exception as exc:  # noqa: BLE001 - pypdf raises several types here
            raise PermissionError("PDF is password protected") from exc
    pages: list[str] = []
    length = 0
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        pages.append(text)
        length += len(text) + 2
        if length > limit:
            break
    return clip_text("\n\n".join(pages).strip(), limit)


def extract_document(name: str, mime: str | None, data: bytes, *, limit: int = MAX_TEXT_CHARS) -> ExtractResult:
    """Text from ``data``; images ask for vision; unreadable or unknown types set ``error``."""
    kind = guess_mime(name, mime)
    if kind.startswith("image/"):
        if kind in VISION_MIMES:
            return _result(needs_vision=True)
        return _result(error="unsupported image type")
    try:
        if kind == PDF_MIME or _suffix(name) == ".pdf":
            text, truncated = _pdf_text(data, limit)
            if not text:
                return _result(error="PDF has no extractable text (it may be scanned)")
            return _result(text, truncated=truncated)
        if kind == DOCX_MIME or _suffix(name) == ".docx":
            text, truncated = clip_text(_docx_text(data), limit)
            return _result(text, truncated=truncated)
        if _is_text(name, kind):
            text, truncated = clip_text(_decode(data), limit)
            return _result(text, truncated=truncated)
    except PermissionError as exc:
        return _result(error=str(exc))
    except Exception:  # noqa: BLE001 - malformed files of any kind become an error record
        return _result(error="file could not be read")
    return _result(error="unsupported type")
