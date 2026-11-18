"""Figma frames as design references, through the REST API with a read-only token.

A Figma link in a request (``parse_figma_urls``) becomes one reference per frame: a PNG
rendered by Figma and a text summary (size, text content, components, colors, fonts). A link
to a frame gives that frame; a link to a page or a whole file gives its top-level frames, up
to ``MAX_FRAMES``.

Each file version is fetched once: results are cached in the intake bucket under
``design/figma/<file>/<version>/``, so the PRD, planning, coding and QA steps, and later
requests for the same version, cost no further API calls. The token is ``FIGMA_TOKEN``
(Secrets Manager ``<prefix>/figma-token``). Without it, a Figma link is reported as not
connected and stays plain text. See docs/runbooks/design-references.md.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import httpx

from ts_harness.intake.design import DESIGN_PREFIX, store_design_image

API = "https://api.figma.com/v1"
MAX_FRAMES = 12
IMAGE_SCALE = 1
# Screens. Sections, pages and the document are containers: the frames inside them are read.
_FRAME_TYPES = {"FRAME", "COMPONENT", "COMPONENT_SET", "INSTANCE"}
_CONTAINER_TYPES = {"DOCUMENT", "CANVAS", "SECTION", "GROUP"}
_TIMEOUT = 30.0
_RENDER_TIMEOUT = 120.0
_RENDER_WORKERS = 4
_STRUCTURE_DEPTH = 5


class FigmaError(Exception):
    """A Figma link that can't be read; the message is shown to the requester."""


def figma_token() -> str:
    return os.environ.get("FIGMA_TOKEN", "").strip()


class FigmaClient:
    def __init__(self, token: str, *, http: httpx.Client | None = None) -> None:
        self.token = token
        self.http = http or httpx.Client(timeout=_TIMEOUT, follow_redirects=True)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        try:
            response = self.http.get(
                f"{API}{path}", params=params, headers={"X-Figma-Token": self.token},
                **({"timeout": timeout} if timeout else {}),
            )
        except httpx.TimeoutException as exc:
            raise FigmaError("Figma did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise FigmaError(f"Figma could not be reached: {exc}") from exc
        if response.status_code == 429:
            wait = response.headers.get("Retry-After", "")
            kind = response.headers.get("X-Figma-Rate-Limit-Type", "")
            raise FigmaError(
                "Figma rate limit reached" + (f" ({kind})" if kind else "") + (f"; retry after {wait}s" if wait else "")
            )
        if response.status_code in (401, 403):
            raise FigmaError("Figma file is not shared with the harness's Figma account")
        if response.status_code == 404:
            raise FigmaError("Figma file or frame not found")
        if response.status_code >= 400:
            raise FigmaError(f"Figma API error {response.status_code}")
        return response.json()

    def nodes(self, file_key: str, node_id: str, *, depth: int | None = 2) -> dict[str, Any]:
        params: dict[str, Any] = {"ids": node_id}
        if depth:
            params["depth"] = depth
        return self._get(f"/files/{file_key}/nodes", params)

    def file(self, file_key: str, *, depth: int = 2) -> dict[str, Any]:
        return self._get(f"/files/{file_key}", {"depth": depth})

    def render(self, file_key: str, node_ids: list[str]) -> dict[str, str]:
        out = self._get(
            f"/images/{file_key}",
            {"ids": ",".join(node_ids), "format": "png", "scale": IMAGE_SCALE},
            timeout=_RENDER_TIMEOUT,
        )
        if out.get("err"):
            raise FigmaError(f"Figma could not render the frames: {out['err']}")
        return {k: v for k, v in (out.get("images") or {}).items() if v}

    def download(self, url: str) -> bytes:
        response = self.http.get(url)
        if response.status_code >= 400:
            raise FigmaError(f"Figma image download failed ({response.status_code})")
        return response.content


def _frames_under(node: dict[str, Any], limit: int = MAX_FRAMES) -> tuple[list[dict[str, Any]], int]:
    """``(frames, total)``: the screens a link points at, opening sections and pages.

    A frame link is that frame. A page, a section (however deeply nested) or the whole file
    gives the frames inside, in Figma's order; ``total`` counts them all, ``frames`` holds the
    first ``limit``.
    """
    found: list[dict[str, Any]] = []
    total = 0

    def walk(item: dict[str, Any]) -> None:
        nonlocal total
        kind = item.get("type")
        if kind in _FRAME_TYPES:
            total += 1
            if len(found) < limit:
                found.append(item)
            return
        if kind in _CONTAINER_TYPES or item is node:
            for child in item.get("children") or []:
                if child.get("visible") is not False:
                    walk(child)

    walk(node)
    return found, total


def _hex(color: dict[str, Any]) -> str:
    return "#" + "".join(f"{round(float(color.get(c, 0)) * 255):02x}" for c in ("r", "g", "b"))


def summarize_frame(frame: dict[str, Any], *, max_texts: int = 40) -> str:
    """Size, text content, components, colors and fonts of a frame, for prompts."""
    texts: list[str] = []
    components: list[str] = []
    colors: list[str] = []
    fonts: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        if node.get("visible") is False:
            return
        kind = node.get("type")
        if kind == "TEXT" and node.get("characters") and len(texts) < max_texts:
            texts.append(" ".join(str(node["characters"]).split())[:160])
            style = node.get("style") or {}
            font = f"{style.get('fontFamily')} {style.get('fontWeight')} {style.get('fontSize')}px".strip()
            if style.get("fontFamily") and font not in fonts and len(fonts) < 8:
                fonts.append(font)
        if kind == "INSTANCE" and node.get("name") and node["name"] not in components and len(components) < 20:
            components.append(str(node["name"]))
        for fill in node.get("fills") or []:
            if fill.get("type") == "SOLID" and fill.get("color") and len(colors) < 12:
                value = _hex(fill["color"])
                if value not in colors:
                    colors.append(value)
        for child in node.get("children") or []:
            walk(child)

    walk(frame)
    box = frame.get("absoluteBoundingBox") or {}
    lines = [f"Frame: {frame.get('name')} ({round(box.get('width', 0))}x{round(box.get('height', 0))})"]
    if texts:
        lines.append("Text: " + " | ".join(texts))
    if components:
        lines.append("Components: " + ", ".join(components))
    if colors:
        lines.append("Colors: " + ", ".join(colors))
    if fonts:
        lines.append("Fonts: " + ", ".join(fonts))
    return "\n".join(lines)


def _frame_url(file_key: str, node_id: str) -> str:
    return f"https://www.figma.com/design/{file_key}/?node-id={node_id.replace(':', '-')}"


class FigmaSource:
    """``frames(pointer)`` → design references (without ids) for one Figma link."""

    def __init__(self, client: FigmaClient, store: Any, *, tenant_id: str) -> None:
        self.client = client
        self.store = store
        self.tenant_id = tenant_id

    def _cache_key(self, file_key: str, version: str, node_id: str) -> str:
        return f"{DESIGN_PREFIX}figma/{file_key}/{version}/{node_id.replace(':', '-')}.json"

    def _cached(self, key: str) -> dict[str, Any] | None:
        try:
            if self.store.head(key) is None:
                return None
            return json.loads(self.store.get(key))
        except Exception:  # noqa: BLE001
            return None

    def frames(self, pointer: dict[str, Any]) -> list[dict[str, Any]]:
        file_key = str(pointer.get("file_key") or "")
        node_id = str(pointer.get("node_id") or "")
        if not file_key:
            raise FigmaError("not a Figma file link")
        if node_id:
            payload = self.client.nodes(file_key, node_id, depth=_STRUCTURE_DEPTH)
            entry = (payload.get("nodes") or {}).get(node_id) or {}
            root = entry.get("document")
            if not root:
                raise FigmaError("Figma frame not found")
        else:
            payload = self.client.file(file_key, depth=_STRUCTURE_DEPTH)
            root = payload.get("document") or {}
        version = str(payload.get("version") or payload.get("lastModified") or "latest")
        frames, total = _frames_under(root)
        if not frames:
            raise FigmaError("no frames at this Figma link")

        refs: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for frame in frames:
            cached = self._cached(self._cache_key(file_key, version, str(frame.get("id"))))
            if cached:
                refs.append(cached)
            else:
                missing.append(frame)
        notes: list[str] = []
        if missing:
            full = self._full_frames(file_key, [str(f.get("id")) for f in missing])
            for frame, image in self._render_each(file_key, missing, notes):
                frame_id = str(frame.get("id"))
                detail = full.get(frame_id) or frame
                ref = {
                    "title": str(frame.get("name") or "Figma frame"),
                    "source": "figma",
                    "url": _frame_url(file_key, frame_id),
                    "mime": "image/png",
                    "object_key": store_design_image(self.store, tenant_id=self.tenant_id, data=image, mime="image/png"),
                    "summary": summarize_frame(detail),
                    "figma": {"file_key": file_key, "node_id": frame_id, "version": version},
                }
                self.store.put(self._cache_key(file_key, version, frame_id), json.dumps(ref).encode())
                refs.append(ref)
        if not refs:
            raise FigmaError("Figma rendered none of the frames: " + "; ".join(notes))
        order = {str(f.get("id")): i for i, f in enumerate(frames)}
        refs.sort(key=lambda r: order.get(str((r.get("figma") or {}).get("node_id")), 0))
        if total > len(frames):
            notes.append(
                f"this link has {total} frames; the first {len(frames)} were read. Link a section or "
                "single frames to read others"
            )
        if notes:
            refs[0] = {**refs[0], "summary": "Note: " + ". ".join(notes) + ".\n" + str(refs[0].get("summary") or "")}
        return refs

    def _full_frames(self, file_key: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        """The frames with all their layers, for summaries (the structure fetch stops early)."""
        try:
            payload = self.client.nodes(file_key, ",".join(ids), depth=None)
        except FigmaError:
            return {}
        return {key: (value or {}).get("document") or {} for key, value in (payload.get("nodes") or {}).items()}

    def _render_each(self, file_key: str, frames: list[dict[str, Any]], notes: list[str]):
        """Render frames one per request, a few at a time: one slow frame must not sink the rest."""
        from concurrent.futures import ThreadPoolExecutor

        def one(frame: dict[str, Any]) -> tuple[dict[str, Any], bytes | None, str]:
            frame_id = str(frame.get("id"))
            try:
                urls = self.client.render(file_key, [frame_id])
                if frame_id not in urls:
                    return frame, None, f"{frame.get('name')}: Figma returned no image"
                return frame, self.client.download(urls[frame_id]), ""
            except FigmaError as exc:
                return frame, None, f"{frame.get('name')}: {exc}"

        with ThreadPoolExecutor(max_workers=min(_RENDER_WORKERS, len(frames))) as pool:
            for frame, image, problem in pool.map(one, frames):
                if image:
                    yield frame, image
                else:
                    notes.append(problem)


def build_figma_source(*, store: Any, tenant_id: str, token: str | None = None) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """The ``figma`` callable for ``resolve_intake_sources``; raises FigmaError when not connected."""
    token = figma_token() if token is None else token
    if not token:

        def not_connected(_pointer: dict[str, Any]) -> list[dict[str, Any]]:
            raise FigmaError("Figma is not connected (no figma-token secret); the link was kept as text")

        return not_connected
    return FigmaSource(FigmaClient(token), store, tenant_id=tenant_id).frames
