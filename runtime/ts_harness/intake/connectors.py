"""Production clients for ``resolve_intake_sources``: chat downloads, Google Drive, image description.

These run in the workflow runner (the gather barrier). They need, in that process:

- ``SLACK_BOT_TOKEN`` (with ``files:read``) for Slack files
- ``TELEGRAM_BOT_TOKEN`` for Telegram files
- the intake object store settings (``TS_HARNESS_OBJECT_BACKEND``, ``TS_HARNESS_INTAKE_BUCKET``)
- ``TS_HARNESS_CONFIG`` and the Google Workspace service account for Drive
- images are read by the stage agent's current model (YAML plus any admin-console overlay);
  ``TS_HARNESS_INTAKE_VISION_MODEL`` overrides it, and ``off`` turns image reading off
"""

from __future__ import annotations

import base64
import functools
import logging
import os
from typing import Any, Callable

import httpx

from ts_harness.intake.blobs import clean_name, load_bytes
from ts_harness.intake.resolve import (
    EXPIRED,
    DescribeImage,
    DriveAccessError,
    SourceUnavailable,
)
from ts_harness.intake.sources import MAX_FILE_BYTES, guess_mime
from ts_harness.storage.objects import ObjectNotFound

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_VISION_BYTES = 5 * 1024 * 1024
VISION_PROMPT = (
    "This image was shared as part of a project brief. Transcribe any visible text verbatim, then "
    "describe what the image shows: screens, UI elements, diagrams, charts, or data. Be factual and "
    "do not guess at intent."
)


# ---------------------------------------------------------------------------
# Chat file downloads
# ---------------------------------------------------------------------------


def fetch_capped(
    client: httpx.Client, url: str, *, headers: dict[str, str] | None = None, max_bytes: int = MAX_FILE_BYTES
) -> bytes:
    """GET ``url`` and stop reading past ``max_bytes``."""
    with client.stream("GET", url, headers=headers or {}, follow_redirects=True) as response:
        if response.status_code in {401, 403, 404, 410}:
            raise SourceUnavailable(EXPIRED)
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise SourceUnavailable("file is larger than 20 MB")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise SourceUnavailable("file is larger than 20 MB")
            chunks.append(chunk)
    return b"".join(chunks)


def _slack(client: httpx.Client, pointer: dict[str, Any]) -> tuple[str, str, bytes]:
    token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    if not token:
        raise SourceUnavailable("Slack files cannot be read: SLACK_BOT_TOKEN is not set")
    headers = {"Authorization": f"Bearer {token}"}
    info = client.get("https://slack.com/api/files.info", params={"file": pointer.get("file_id")}, headers=headers).json()
    if not info.get("ok"):
        error = str(info.get("error") or "")
        if error == "missing_scope":
            raise SourceUnavailable("the Slack app needs the files:read scope")
        raise SourceUnavailable("file is no longer available in Slack")
    meta = info.get("file") or {}
    if int(meta.get("size") or 0) > MAX_FILE_BYTES:
        raise SourceUnavailable("file is larger than 20 MB")
    url = str(meta.get("url_private_download") or meta.get("url_private") or "")
    if not url:
        raise SourceUnavailable("file is no longer available in Slack")
    name = clean_name(str(meta.get("name") or pointer.get("name") or "attachment"))
    return name, guess_mime(name, str(meta.get("mimetype") or pointer.get("mime") or "")), fetch_capped(client, url, headers=headers)


def _telegram(client: httpx.Client, pointer: dict[str, Any]) -> tuple[str, str, bytes]:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SourceUnavailable("Telegram files cannot be read: TELEGRAM_BOT_TOKEN is not set")
    info = client.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": pointer.get("file_id")}).json()
    result = info.get("result") or {}
    if not info.get("ok") or not result.get("file_path"):
        raise SourceUnavailable("file is no longer available in Telegram")
    if int(result.get("file_size") or 0) > MAX_FILE_BYTES:
        raise SourceUnavailable("file is larger than 20 MB")
    name = clean_name(str(pointer.get("name") or result["file_path"].rsplit("/", 1)[-1]))
    data = fetch_capped(client, f"https://api.telegram.org/file/bot{token}/{result['file_path']}")
    return name, guess_mime(name, str(pointer.get("mime") or "")), data


def _discord(client: httpx.Client, pointer: dict[str, Any]) -> tuple[str, str, bytes]:
    """The prefetched copy when it exists, else Discord's (expiring) CDN URL."""
    name = clean_name(str(pointer.get("name") or "attachment"))
    mime = guess_mime(name, str(pointer.get("mime") or ""))
    if pointer.get("object_key"):
        try:
            return name, mime, load_bytes({"object_key": pointer["object_key"]})
        except ObjectNotFound:
            pass
    url = str(pointer.get("url") or "")
    if not url:
        raise SourceUnavailable(EXPIRED)
    return name, mime, fetch_capped(client, url)


def build_download(client: httpx.Client | None = None) -> Callable[[dict[str, Any]], tuple[str, str, bytes]]:
    http = client or httpx.Client(timeout=_TIMEOUT)

    def download(pointer: dict[str, Any]) -> tuple[str, str, bytes]:
        kind = str(pointer.get("kind") or "")
        if kind == "stored":
            name = clean_name(str(pointer.get("name") or "attachment"))
            return name, guess_mime(name, str(pointer.get("mime") or "")), load_bytes(pointer)
        fetch = {"slack_file": _slack, "telegram_file": _telegram, "discord_file": _discord}.get(kind)
        if fetch is None:
            raise SourceUnavailable("unsupported attachment")
        try:
            return fetch(http, pointer)
        except httpx.HTTPStatusError as exc:
            # The message would carry the URL, which for Telegram embeds the bot token.
            raise SourceUnavailable(f"download failed (HTTP {exc.response.status_code})") from None
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"download failed ({type(exc).__name__})") from None

    return download


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


def _http_status(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class GoogleDriveSource:
    """``DriveSource`` over the Drive v3 API, read as the stage's connector mailbox."""

    _FIELDS = "id,name,mimeType,size,webViewLink"

    def __init__(self, drive_api: Any) -> None:
        self.api = drive_api

    def _call(self, request: Any) -> Any:
        try:
            return request.execute()
        except Exception as exc:
            if _http_status(exc) in {403, 404}:
                raise DriveAccessError() from exc
            raise

    def metadata(self, file_id: str) -> dict[str, Any]:
        return self._call(self.api.files().get(fileId=file_id, fields=self._FIELDS, supportsAllDrives=True))

    def export(self, file_id: str, mime: str) -> bytes:
        data = self._call(self.api.files().export(fileId=file_id, mimeType=mime))
        return data if isinstance(data, bytes) else str(data or "").encode()

    def download(self, file_id: str) -> bytes:
        return self._call(self.api.files().get_media(fileId=file_id, supportsAllDrives=True))

    def list_children(self, folder_id: str, limit: int) -> list[dict[str, Any]]:
        payload = self._call(
            self.api.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields=f"files({self._FIELDS})",
                pageSize=limit,
                orderBy="name",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        return list((payload or {}).get("files") or [])[:limit]


@functools.lru_cache(maxsize=4)
def _runtime_config(path: str) -> Any:
    from ts_harness.config.schema import load_runtime_config

    return load_runtime_config(path)


def build_drive(*, tenant_id: str, agent_id: str) -> GoogleDriveSource | None:
    """Drive as ``agent_id``'s mailbox, or ``None`` when Google Workspace is not configured."""
    path = (os.environ.get("TS_HARNESS_CONFIG") or "").strip()
    if not path:
        return None
    try:
        from ts_harness.connectors.google_workspace.doc_helpers import build_gw_clients

        clients = build_gw_clients(_runtime_config(path), tenant_id=tenant_id, agent_id=agent_id, source="intake_sources")
    except Exception as exc:  # noqa: BLE001 - Drive pointers then report "not connected"
        logger.warning("intake drive client unavailable: %s", exc)
        return None
    if clients is None:
        return None
    return GoogleDriveSource(clients[0])


# ---------------------------------------------------------------------------
# Image description
# ---------------------------------------------------------------------------


def _image_message(api_mode: str, mime: str, data: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode()
    if api_mode == "anthropic":
        content: list[dict[str, Any]] = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}},
            {"type": "text", "text": VISION_PROMPT},
        ]
    else:
        content = [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ]
    return {"role": "user", "content": content}


_VISION_OFF = frozenset({"off", "false", "none", "0", "disabled"})


def agent_model(agent_id: str) -> str | None:
    """``agent_id``'s model as configured now: the runtime YAML with the active agents overlay."""
    path = (os.environ.get("TS_HARNESS_CONFIG") or "").strip()
    if not path:
        return None
    try:
        from ts_harness.config.agents_overlay import DEFINITION_TYPE, merge_agents

        baseline = list(_runtime_config(path).agents)
        try:
            from ts_harness.workflows.agents_overlay_store import agents_overlay_store

            overlay = agents_overlay_store().get_active(DEFINITION_TYPE)
        except Exception as exc:  # noqa: BLE001 - no overlay store means YAML only
            logger.debug("intake: no agents overlay (%s)", exc)
            overlay = None
        for agent in merge_agents(baseline, overlay):
            if agent.id == agent_id:
                return str(agent.model or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - image reading then reports "not enabled"
        logger.warning("intake: could not resolve the model for %s: %s", agent_id, exc)
    return None


def vision_model(agent_id: str) -> str | None:
    """The override when set (``off`` disables), else the stage agent's model."""
    setting = (os.environ.get("TS_HARNESS_INTAKE_VISION_MODEL") or "").strip()
    if setting.lower() in _VISION_OFF:
        return None
    return setting or agent_model(agent_id)


def build_image_describer(*, agent_id: str = "pm-agent", scope: dict[str, Any] | None = None) -> DescribeImage | None:
    """A describer using ``vision_model(agent_id)``, or ``None`` when image reading is off."""
    model = vision_model(agent_id)
    if not model:
        return None
    from ts_harness.agent.provider import build_provider

    def describe(name: str, mime: str, data: bytes) -> str:
        if len(data) > MAX_VISION_BYTES:
            raise SourceUnavailable("image is larger than 5 MB and was not read")
        provider = build_provider(model, scope=scope)
        api_mode = str(getattr(getattr(provider, "endpoint", None), "api_mode", "openai"))
        response = provider.chat([_image_message(api_mode, mime, data)], temperature=0)
        text = str(((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise SourceUnavailable("image could not be read")
        return text

    return describe
