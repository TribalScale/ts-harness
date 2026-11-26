"""Each design frame next to its capture, with colours sampled from both, for the QA gate.

The coding job captures every frame's scenario at the frame's own size (``frame-D7.png`` on the
evidence commit). Here each capture is laid next to its frame in one image, labelled, so QA
compares like with like. Both images are also sampled on a grid: where the design is a flat
colour (a background, a header row, a panel), the colour at the same place in the capture is
read. Places where they disagree are grouped by colour pair, which is how a beige page behind
a white design, or white header rows where the design is grey, shows up as data and not as
an impression. A layout that moved things can explain a pair: QA checks the images.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Composite images per QA call (one per frame); the work order names at most this many frames.
MAX_COMPOSITES = 6
COMPOSITE_WIDTH = 2400
COMPOSITE_BYTES = 5_000_000
GRID_COLUMNS = 24
GRID_ROWS = 16
PATCH = 2  # a sample is the (2*PATCH+1)² block around the point
FLAT_SPREAD = 6  # max channel spread inside a block for it to count as a flat colour
COLOUR_TOLERANCE = 10  # max channel difference for two flat colours to be the same token
MAX_PAIRS = 6
LABEL_HEIGHT = 48


def _image(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data)).convert("RGB")


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _block(
    pixels: Any, x: int, y: int, width: int, height: int
) -> tuple[tuple[int, int, int], int] | None:
    values = [
        pixels[min(width - 1, max(0, x + dx)), min(height - 1, max(0, y + dy))]
        for dx in range(-PATCH, PATCH + 1)
        for dy in range(-PATCH, PATCH + 1)
    ]
    spread = max(max(v[c] for v in values) - min(v[c] for v in values) for c in range(3))
    mid = tuple(sorted(v[c] for v in values)[len(values) // 2] for c in range(3))
    return mid, spread  # type: ignore[return-value]


def sample_colours(design: Any, capture: Any) -> dict[str, Any]:
    """Grid-sample both images (capture scaled to the design's size) and group colour mismatches.

    Only points where **both** images are a flat colour count: text and icons are skipped, so
    a mismatch is a different fill, not antialiasing.
    """
    if capture.size != design.size:
        capture = capture.resize(design.size)
    width, height = design.size
    d_px, c_px = design.load(), capture.load()
    compared = 0
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for row in range(GRID_ROWS):
        for col in range(GRID_COLUMNS):
            x = int((col + 0.5) * width / GRID_COLUMNS)
            y = int((row + 0.5) * height / GRID_ROWS)
            d_rgb, d_spread = _block(d_px, x, y, width, height)  # type: ignore[misc]
            c_rgb, c_spread = _block(c_px, x, y, width, height)  # type: ignore[misc]
            if d_spread > FLAT_SPREAD or c_spread > FLAT_SPREAD:
                continue
            compared += 1
            if max(abs(d_rgb[i] - c_rgb[i]) for i in range(3)) <= COLOUR_TOLERANCE:
                continue
            key = (_hex(d_rgb), _hex(c_rgb))
            entry = pairs.setdefault(
                key, {"design": key[0], "capture": key[1], "points": 0, "examples": []}
            )
            entry["points"] += 1
            if len(entry["examples"]) < 4:
                entry["examples"].append({"x": x, "y": y})
    ranked = sorted(pairs.values(), key=lambda item: -item["points"])[:MAX_PAIRS]
    return {"compared_points": compared, "mismatches": ranked}


def _label_strip(width: int, text: str):
    from PIL import Image, ImageDraw, ImageFont

    strip = Image.new("RGB", (width, LABEL_HEIGHT), (26, 26, 26))
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:  # Pillow before 10.1 has one fixed-size default font
        font = ImageFont.load_default()
    ImageDraw.Draw(strip).text((14, 10), text, fill=(255, 255, 255), font=font)
    return strip


def side_by_side(design: Any, capture: Any, *, left: str, right: str) -> bytes:
    """``design | capture`` at the design's size, labelled, as a JPEG no wider than ``COMPOSITE_WIDTH``."""
    from PIL import Image

    if capture.size != design.size:
        capture = capture.resize(design.size)
    width, height = design.size
    gap = 16
    canvas = Image.new("RGB", (width * 2 + gap, height + LABEL_HEIGHT), (255, 255, 255))
    canvas.paste(_label_strip(width, left), (0, 0))
    canvas.paste(_label_strip(width, right), (width + gap, 0))
    canvas.paste(design, (0, LABEL_HEIGHT))
    canvas.paste(capture, (width + gap, LABEL_HEIGHT))
    if canvas.width > COMPOSITE_WIDTH:
        canvas = canvas.resize(
            (COMPOSITE_WIDTH, round(canvas.height * COMPOSITE_WIDTH / canvas.width))
        )
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=82)
    return out.getvalue()


def _fonts(ref: dict[str, Any]) -> str:
    for line in str(ref.get("summary") or "").splitlines():
        if line.startswith("Fonts:"):
            return line[len("Fonts:") :].strip()
    return ""


def frame_review_input(
    refs: list[dict[str, Any]],
    captures: list[dict[str, Any]] | None,
    store: Any,
    *,
    github: Any = None,
) -> dict[str, Any]:
    """``{frame_captures, design_checks, _images}`` for the QA step; ``{}`` without captures.

    ``refs`` are the work order's design references (with ``object_key``); ``captures`` the
    coding stage's ``frame_captures``. A frame with no published capture keeps its status and
    note, so QA can fail it by name.
    """
    if not captures:
        return {}
    by_id = {str(c.get("id")): c for c in captures if isinstance(c, dict) and c.get("id")}
    client = github
    out_captures: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    images: list[dict[str, str]] = []
    used = 0
    for ref in refs[:MAX_COMPOSITES]:
        ref_id = str(ref.get("id") or "")
        capture = dict(
            by_id.get(ref_id) or {"id": ref_id, "status": "missing", "note": "no capture reported"}
        )
        capture.setdefault("title", ref.get("title") or "")
        entry = {
            k: capture.get(k)
            for k in ("id", "title", "status", "note", "width", "height", "scenario")
            if capture.get(k)
        }
        if capture.get("file") and capture.get("commit") and "/" in str(capture.get("repo") or ""):
            try:
                if client is None:
                    from ts_harness.integrations.github_client import GitHubClient

                    client = GitHubClient.from_env()
                owner, repo = str(capture["repo"]).split("/", 1)
                payload = (
                    client.get_file(owner, repo, str(capture["file"]), ref=str(capture["commit"]))
                    or {}
                )
                shot = _image(base64.b64decode(str(payload.get("content") or "").replace("\n", "")))
                design = _image(store.get(str(ref.get("object_key"))))
                sampled = sample_colours(design, shot)
                checks.append(
                    {
                        "id": ref_id,
                        "title": entry.get("title", ""),
                        "compared_points": sampled["compared_points"],
                        "colour_mismatches": sampled["mismatches"],
                        "design_fonts": _fonts(ref),
                        "note": "sampled at the same places in both images; points on text or icons are skipped",
                    }
                )
                composite = side_by_side(
                    design,
                    shot,
                    left=f"Design {ref_id}: {entry.get('title', '')}"[:120],
                    right=f"Capture {ref_id} (the app)",
                )
                encoded = base64.b64encode(composite).decode("ascii")
                if used + len(encoded) <= COMPOSITE_BYTES:
                    used += len(encoded)
                    images.append(
                        {
                            "name": f"frame-{ref_id}",
                            "label": f"Design {ref_id} (left) and Capture {ref_id} (right)",
                            "url": f"data:image/jpeg;base64,{encoded}",
                        }
                    )
                    entry["image"] = f"frame-{ref_id}"
            except Exception as exc:  # noqa: BLE001 - QA still sees the status and the separate images
                logger.warning("frame review for %s failed: %s", ref_id, exc)
                entry["review_error"] = str(exc)[:200]
        out_captures.append(entry)
    return {"frame_captures": out_captures, "design_checks": checks, "_images": images}
