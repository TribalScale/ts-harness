"""Design references: images of what to build, from Figma frames or images attached to a request.

A design reference is a small dict carried in program artifacts (never the bytes)::

    {"id": "D1", "title": "Checkout / Payment", "source": "figma" | "upload",
     "url": "https://www.figma.com/design/...", "mime": "image/png",
     "object_key": "design/<tenant>/<sha256>.png", "summary": "frame text, size, components..."}

The images live in the intake bucket under ``design/``, which is kept for a year (``intake/``
expires after 14 days), so they outlive the delivery. The PRD and planning steps see every
reference; a work order's coding job and QA see the references its ``Design`` row names
(``| Design | D1, D3 |``). See docs/runbooks/design-references.md.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

DESIGN_PREFIX = "design/"
MAX_DESIGN_REFS = 12
# Images sent with one agent call: the gateway takes JSON bodies up to 10 MB.
AGENT_IMAGE_LIMIT = 6
AGENT_IMAGE_BYTES = 6_000_000
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_DESIGN_ROW = re.compile(r"\|\s*\*{0,2}Design\*{0,2}\s*\|\s*([^|\n]+)\|", re.IGNORECASE)
_DESIGN_LINE = re.compile(r"^\s*(?:[-*]\s+)?\*{0,2}Design\*{0,2}\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_REF_ID = re.compile(r"\bD(\d{1,2})\b")


def _segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "default")).strip("-") or "default"


def design_key(*, tenant_id: str, data: bytes, mime: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"{DESIGN_PREFIX}{_segment(tenant_id)}/{digest}.{_EXTENSIONS.get(mime, 'bin')}"


def store_design_image(store: Any, *, tenant_id: str, data: bytes, mime: str) -> str:
    """Put the image under ``design/`` (content-addressed, so storing twice is free); return its key."""
    key = design_key(tenant_id=tenant_id, data=data, mime=mime)
    try:
        if store.head(key) is not None:
            return key
    except Exception:  # noqa: BLE001 - a failed head just means we write again
        pass
    store.put(key, data)
    return key


def number_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give references stable ids D1..Dn in order, dropping duplicates of the same image."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = str(ref.get("object_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({**ref, "id": f"D{len(out) + 1}"})
        if len(out) >= MAX_DESIGN_REFS:
            break
    return out


def render_design_refs(refs: list[dict[str, Any]], *, heading: str = "## Design references") -> str:
    """Markdown naming each reference, for prompts; the images themselves follow the message."""
    if not refs:
        return ""
    lines = [
        heading,
        "",
        "Images of what to build. They follow this message in the same order; refer to them by id.",
        "",
    ]
    for ref in refs:
        title = f"- **{ref.get('id')}** {ref.get('title') or 'design'}"
        if ref.get("url"):
            title += f" ({ref['url']})"
        title += " — Figma frame" if ref.get("source") == "figma" else " — attached image"
        lines.append(title)
        summary = str(ref.get("summary") or "").strip()
        if summary:
            lines.extend(f"  {line}" for line in summary.splitlines()[:12])
    return "\n".join(lines).rstrip() + "\n"


def refs_named_by_work_order(refs: list[dict[str, Any]], work_order_body: str) -> list[dict[str, Any]]:
    """The references a work order's ``Design`` row (or ``Design:`` line) names, in its order."""
    text = str(work_order_body or "")
    match = _DESIGN_ROW.search(text) or _DESIGN_LINE.search(text)
    if not match:
        return []
    by_id = {str(ref.get("id") or "").upper(): ref for ref in refs}
    wanted: list[dict[str, Any]] = []
    for number in _REF_ID.findall(match.group(1)):
        ref = by_id.get(f"D{int(number)}")
        if ref is not None and ref not in wanted:
            wanted.append(ref)
    return wanted


def public_ref(ref: dict[str, Any]) -> dict[str, Any]:
    """A reference without storage details, for prompts."""
    return {k: ref.get(k) for k in ("id", "title", "source", "url", "summary") if ref.get(k)}


def load_images(
    refs: list[dict[str, Any]],
    store: Any,
    *,
    limit: int = AGENT_IMAGE_LIMIT,
    max_bytes: int = AGENT_IMAGE_BYTES,
) -> list[dict[str, str]]:
    """``[{"name", "url": data-url}]`` for the agent loop's ``_images``, within the caps.

    A reference whose image can't be read is skipped (its text summary still goes to the agent).
    """
    images: list[dict[str, str]] = []
    used = 0
    for ref in refs[:limit]:
        key = str(ref.get("object_key") or "")
        if not key:
            continue
        try:
            data = store.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("design image %s unavailable: %s", key, exc)
            continue
        encoded = base64.b64encode(data).decode("ascii")
        if used + len(encoded) > max_bytes:
            break
        used += len(encoded)
        mime = str(ref.get("mime") or "image/png")
        images.append(
            {
                "name": f"{ref.get('id')} {ref.get('title') or ''}".strip(),
                "label": f"Design {ref.get('id')}: {ref.get('title') or 'design'}",
                "url": f"data:{mime};base64,{encoded}",
            }
        )
    return images
