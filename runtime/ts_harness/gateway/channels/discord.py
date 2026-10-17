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
