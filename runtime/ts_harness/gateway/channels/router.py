from __future__ import annotations

from ts_harness.gateway.channels.discord import DiscordAdapter
from ts_harness.gateway.channels.slack import SlackAdapter
from ts_harness.gateway.channels.telegram import TelegramAdapter


class ChannelRouter:
    def __init__(self, adapters: dict | None = None):
        self._adapters = adapters or {
            "slack": SlackAdapter(),
            "discord": DiscordAdapter(),
            "telegram": TelegramAdapter(),
        }

    def send(self, channel: str, args: dict, *, tenant_id: str) -> dict:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return {"error": f"unknown channel: {channel}"}
        return adapter.send(args, tenant_id=tenant_id)

    def normalize(self, channel: str, payload: dict, *, tenant_id: str):
        adapter = self._adapters.get(channel)
        if adapter is None:
            raise ValueError(f"unknown channel: {channel}")
        return adapter.normalize_inbound(payload, tenant_id=tenant_id)


_router = ChannelRouter()


def get_channel_router() -> ChannelRouter:
    return _router
