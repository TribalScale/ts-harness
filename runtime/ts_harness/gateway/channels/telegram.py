from __future__ import annotations

import os
from typing import Any

from ts_harness.gateway.channels.base import BaseChannelAdapter, JsonHttpTransport, MessageEvent


class TelegramAdapter(BaseChannelAdapter):
    name = "telegram"

    def __init__(self, bot_token: str | None = None, transport: JsonHttpTransport | None = None):
        self.bot_token = bot_token if bot_token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.transport = transport or JsonHttpTransport()

    def normalize_inbound(self, payload: dict[str, Any], *, tenant_id: str) -> MessageEvent:
        message = payload.get("message") or {}
        if message.get("from", {}).get("is_bot"):
            raise ValueError("bot loop suppressed")
        return MessageEvent(
            channel="telegram",
            tenant_id=tenant_id,
            text=str(message.get("text") or message.get("caption") or ""),
            sender_id=str(message.get("from", {}).get("id", "unknown")),
            thread_id=str(message.get("chat", {}).get("id", "")),
            metadata=payload,
            attachments=telegram_file_pointers(message),
        )

    def send(self, args: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
        if not self.bot_token:
            return {"error": "TELEGRAM_BOT_TOKEN is required"}
        response = self.transport.post_json(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            {"chat_id": str(args.get("channel", "")), "text": str(args.get("text", ""))},
        )
        return {
            "status": "sent",
            "channel": "telegram",
            "tenant_id": tenant_id,
            "message_id": response.get("result", {}).get("message_id") or response.get("id"),
        }


def telegram_file_pointers(message: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``document``, or the largest ``photo`` size, as a ``telegram_file`` pointer."""
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        return [
            {
                "kind": "telegram_file",
                "file_id": str(document["file_id"]),
                "file_unique_id": str(document.get("file_unique_id") or ""),
                "name": str(document.get("file_name") or "attachment"),
                "mime": str(document.get("mime_type") or ""),
                "size": int(document.get("file_size") or 0),
            }
        ]
    photos = [p for p in message.get("photo") or [] if isinstance(p, dict) and p.get("file_id")]
    if not photos:
        return []
    largest = max(photos, key=lambda p: (int(p.get("file_size") or 0), int(p.get("width") or 0)))
    return [
        {
            "kind": "telegram_file",
            "file_id": str(largest["file_id"]),
            "file_unique_id": str(largest.get("file_unique_id") or ""),
            "name": "photo.jpg",
            "mime": "image/jpeg",
            "size": int(largest.get("file_size") or 0),
        }
    ]
