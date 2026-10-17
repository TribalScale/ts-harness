"""Publish long workflow artifacts to Google Docs for channel presentation."""

from __future__ import annotations

from typing import Any

from ts_harness.config.schema import RuntimeConfig
from ts_harness.connectors.google_workspace.doc_helpers import (
    build_formatted_insert_requests,
    build_gw_clients,
    create_document_with_markdown,
    gw_config,
    share_document,
)

# Backward-compatible re-exports for tests and callers.
_build_formatted_insert_requests = build_formatted_insert_requests
_share_document = share_document


def _publish_settings(runtime_config: RuntimeConfig):
    gw = gw_config(runtime_config)
    if gw is None or not gw.enabled:
        return None
    return gw.artifact_publish


def should_publish_artifact_text(
    text_len: int,
    *,
    max_channel_length: int,
    min_chars: int,
) -> bool:
    if text_len <= 0:
        return False
    if text_len >= min_chars:
        return True
    return text_len > max_channel_length


def publish_markdown_to_google_doc(
    *,
    title: str,
    content: str,
    runtime_config: RuntimeConfig,
    tenant_id: str = "default",
    agent_id: str = "pm-agent",
    share_with_email: str | None = None,
    parent_folder_id: str | None = None,
) -> dict[str, Any]:
    """Create a Google Doc with markdown content and optionally share it."""
    publish_cfg = _publish_settings(runtime_config)
    if publish_cfg is None or not publish_cfg.enabled:
        return {"status": "skipped", "reason": "artifact_publish disabled"}

    body = (content or "").strip()
    if not body:
        return {"status": "skipped", "reason": "empty content"}

    clients = build_gw_clients(
        runtime_config,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    if clients is None:
        return {"status": "error", "error": "google-workspace connector not configured"}

    drive, docs, _factory = clients
    folder_id = parent_folder_id or publish_cfg.folder_id or None
    chunk_chars = max(1000, int(publish_cfg.chunk_chars or 50000))

    try:
        created = create_document_with_markdown(
            drive,
            docs,
            title=title,
            content=body,
            parent_folder_id=folder_id,
            chunk_chars=chunk_chars,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"drive create failed: {exc}"}

    document_id = str(created.get("document_id") or "")
    share_result = share_document(drive, document_id, share_with_email or "")
    return {
        "status": "published",
        "documentId": document_id,
        "documentTitle": created.get("document_title"),
        "webViewLink": created.get("web_view_link"),
        "sharedWith": share_result,
    }
