from __future__ import annotations

import os
from typing import Any

from ts_harness.gateway.channels.base import BaseChannelAdapter, JsonHttpTransport, MessageEvent


class DiscordAdapter(BaseChannelAdapter):
    name = "discord"

    def __init__(self, webhook_url: str | None = None, transport: JsonHttpTransport | None = None):
        self.webhook_url = webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.transport = transport or JsonHttpTransport()

    def normalize_inbound(self, payload: dict[str, Any], *, tenant_id: str) -> MessageEvent:
        if payload.get("author", {}).get("bot"):
            raise ValueError("bot loop suppressed")
        return MessageEvent(
            channel="discord",
            tenant_id=tenant_id,
            text=str(payload.get("content", "")),
            sender_id=str(payload.get("author", {}).get("id", "unknown")),
            thread_id=str(payload.get("channel_id", "")),
            metadata=payload,
            attachments=discord_file_pointers(payload.get("attachments"), tenant_id=tenant_id),
        )

    def send(self, args: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
        if not self.webhook_url:
            return {"error": "DISCORD_WEBHOOK_URL is required"}
        response = self.transport.post_json(
            self.webhook_url,
            {"content": str(args.get("text", ""))},
            headers={"Content-Type": "application/json"},
        )
        return {
            "status": "sent",
            "channel": "discord",
            "tenant_id": tenant_id,
            "message_id": response.get("id"),
        }


def discord_file_pointers(attachments: Any, *, tenant_id: str) -> list[dict[str, Any]]:
    """``discord_file`` pointers. Each names the key its prefetch copy will be stored under."""
    from ts_harness.intake.blobs import discord_key
    from ts_harness.intake.sources import MAX_FILE_BYTES

    out: list[dict[str, Any]] = []
    for item in attachments if isinstance(attachments, list) else []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("url"):
            continue
        size = int(item.get("size") or 0)
        pointer: dict[str, Any] = {
            "kind": "discord_file",
            "attachment_id": str(item["id"]),
            "url": str(item["url"]),
            "name": str(item.get("filename") or "attachment"),
            "mime": str(item.get("content_type") or ""),
            "size": size,
            "object_key": discord_key(tenant_id=tenant_id, attachment_id=str(item["id"])),
        }
        if size > MAX_FILE_BYTES:
            pointer["error"] = "file is larger than 20 MB"
        out.append(pointer)
    return out
