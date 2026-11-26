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
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

DESIGN_PREFIX = "design/"
# A Figma page can hold a hundred screens; planning names the ones each work order builds.
DEFAULT_MAX_DESIGN_REFS = 200
# Beyond this many references, prompts carry a shorter summary of each.
_COMPACT_AFTER = 24
# Images sent with one agent call: the gateway takes JSON bodies up to 10 MB.
AGENT_IMAGE_LIMIT = 6
AGENT_IMAGE_BYTES = 6_000_000
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_DESIGN_ROW = re.compile(r"\|\s*\*{0,2}Design\*{0,2}\s*\|\s*([^|\n]+)\|", re.IGNORECASE)
_DESIGN_LINE = re.compile(r"^\s*(?:[-*]\s+)?\*{0,2}Design\*{0,2}\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_REF_ID = re.compile(r"\bD(\d{1,3})\b")
FRAMES_HEADING = "## Design frames"


def max_design_refs() -> int:
    """References kept per request (``TS_HARNESS_MAX_DESIGN_REFS``)."""
    try:
        value = int(os.environ.get("TS_HARNESS_MAX_DESIGN_REFS", "") or DEFAULT_MAX_DESIGN_REFS)
    except ValueError:
        value = DEFAULT_MAX_DESIGN_REFS
    return max(1, value)


def _identity(ref: dict[str, Any]) -> str:
    """A Figma frame is itself even when it renders like another (two states drawn alike)."""
    figma = ref.get("figma") if isinstance(ref.get("figma"), dict) else {}
    if figma.get("file_key") and figma.get("node_id"):
        return f"figma:{figma['file_key']}:{figma['node_id']}"
    return str(ref.get("object_key") or "")


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
    """Give references stable ids D1..Dn in order, dropping repeats (the same Figma frame, or
    the same attached image)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cap = max_design_refs()
    for ref in refs:
        key = _identity(ref)
        if not str(ref.get("object_key") or "") or key in seen:
            continue
        seen.add(key)
        out.append({**ref, "id": f"D{len(out) + 1}"})
        if len(out) >= cap:
            break
    return out


def render_design_refs(refs: list[dict[str, Any]], *, heading: str = "## Design references") -> str:
    """Markdown naming each reference, for prompts; the images themselves follow the message."""
    if not refs:
        return ""
    compact = len(refs) > _COMPACT_AFTER
    lines = [
        heading,
        "",
        "Images of what to build, with each frame's text. The first images follow this message in "
        "the same order; refer to every frame by id.",
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
            kept = summary.splitlines()[: 4 if compact else 12]
            lines.extend(f"  {line[:400] if compact else line}" for line in kept)
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


_FRAME_SIZE = re.compile(r"^Frame:.*\((\d{2,5})\s*[x×]\s*(\d{2,5})\)\s*$", re.MULTILINE)


def frame_size(ref: dict[str, Any]) -> tuple[int, int] | None:
    """A frame's width and height: its ``width``/``height`` fields, else its summary's ``Frame: name (WxH)``."""
    try:
        if ref.get("width") and ref.get("height"):
            return int(ref["width"]), int(ref["height"])
    except (TypeError, ValueError):
        pass
    match = _FRAME_SIZE.search(str(ref.get("summary") or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


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


def link_design_refs(markdown: str, refs: list[dict[str, Any]] | None) -> str:
    """Work orders with a ``## Design frames`` section linking each frame they name.

    The planner names frames by id (``- Design: D1, D3``); ids mean nothing in a Jira ticket or
    to a reviewer, so each work order gets the frames' titles and Figma links, plus the
    ``Design:`` line the coding job reads. Running it again refreshes the section.
    """
    from ts_harness.implementation.work_order_context import _WO_HEADER

    text = str(markdown or "")
    usable = [ref for ref in refs or [] if isinstance(ref, dict) and ref.get("id")]
    if not text or not usable:
        return text
    matches = list(_WO_HEADER.finditer(text))
    if not matches:
        return text
    pieces = [text[: matches[0].start()]]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pieces.append(_with_frames_section(text[match.start() : end], usable))
    return "".join(pieces)


def _with_frames_section(section: str, refs: list[dict[str, Any]]) -> str:
    trailing = section[len(section.rstrip()) :]
    body = section.rstrip()
    heading_at = body.find("\n" + FRAMES_HEADING)
    if heading_at >= 0:
        rest = body[heading_at + 1 :]
        following = re.search(r"^#{1,3} ", rest[len(FRAMES_HEADING) :], re.MULTILINE)
        tail = rest[len(FRAMES_HEADING) + following.start() :] if following else ""
        body = (body[:heading_at] + ("\n\n" + tail if tail else "")).rstrip()
    named = refs_named_by_work_order(refs, body)
    if not named:
        return body + (trailing or "\n")
    lines = ["", "", FRAMES_HEADING, "", "Design: " + ", ".join(str(r["id"]) for r in named), ""]
    for ref in named:
        title = str(ref.get("title") or "design").replace("[", "(").replace("]", ")")
        size = frame_size(ref)
        # The size is what the harness captures the frame's scenario at, for QA.
        at = f" · {size[0]}×{size[1]}" if size else ""
        if ref.get("url"):
            lines.append(f"- {ref['id']} — [{title}]({ref['url']}){at}")
        else:
            lines.append(f"- {ref['id']} — {title} (attached image){at}")
    return body + "\n".join(lines) + (trailing if trailing.count("\n") > 1 else "\n\n")

